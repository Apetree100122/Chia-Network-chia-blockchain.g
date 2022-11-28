import ast
import dataclasses
import inspect
import math

from blspy import AugSchemeMPL, G1Element, G2Element
from clvm_tools.binutils import disassemble
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from chia.data_layer.data_layer_wallet import UpdateMetadataDL
from chia.types.announcement import Announcement
from chia.types.blockchain_format.coin import Coin, coin_as_list
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32, bytes48
from chia.types.coin_spend import CoinSpend
from chia.types.spend_bundle import SpendBundle
from chia.util.ints import uint16, uint64
from chia.wallet.action_manager.coin_info import CoinInfo
from chia.wallet.db_wallet.db_wallet_puzzles import create_host_fullpuz, GRAFTROOT_DL_OFFERS, RequireDLInclusion
from chia.wallet.outer_puzzles import AssetType
from chia.wallet.payment import Payment
from chia.wallet.puzzle_drivers import cast_to_int, PuzzleInfo, Solver
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import solution_for_delegated_puzzle
from chia.wallet.puzzles.puzzle_utils import (
    make_assert_coin_announcement,
    make_create_coin_announcement,
    make_create_coin_condition,
    make_create_puzzle_announcement,
    make_reserve_fee_condition,
)
from chia.wallet.trading.action_aliases import (
    ActionAlias,
    AssertAnnouncement,
    DirectPayment,
    Fee,
    MakeAnnouncement,
    OfferedAmount,
    RequestPayment,
)
from chia.wallet.trading.offer import ADD_WRAPPED_ANNOUNCEMENT, Offer, OFFER_MOD
from chia.wallet.trading.wallet_actions import WalletAction
from chia.wallet.util.wallet_types import WalletType
from chia.wallet.wallet_protocol import WalletProtocol


async def old_request_to_new(
    wallet_state_manager: Any,
    offer_dict: Dict[Optional[bytes32], int],
    driver_dict: Dict[bytes32, PuzzleInfo],
    solver: Solver,
    fee: uint64,
) -> Tuple[Solver, Dict[bytes32, PuzzleInfo]]:
    """
    This method takes an old style offer dictionary and converts it to a new style action specification
    """
    final_solver: Dict[str, Any] = solver.info

    offered_assets: Dict[Optional[bytes32], int] = {k: v for k, v in offer_dict.items() if v < 0}
    requested_assets: Dict[Optional[bytes32], int] = {k: v for k, v in offer_dict.items() if v > 0}

    # When offers first came out, they only supported CATs and driver_dict did not exist
    # We need to fill in any requested assets that do not exist in driver_dict already as CATs
    cat_assets: Dict[bytes32, PuzzleInfo] = {
        key: PuzzleInfo({"type": AssetType.CAT.value, "tail": "0x" + key.hex()})
        for key in requested_assets
        if key is not None and key not in driver_dict
    }
    driver_dict.update(cat_assets)

    # Keep track of the DL assets since they show up under the offered asset's name
    dl_dependencies: List[Solver] = []
    # DLs need to do an announcement after they update so we'll keep track of those to add at the end
    additional_actions: List[Dict[str, Any]] = []

    final_solver.setdefault("actions", [])
    for asset_id, amount in offered_assets.items():

        # Get the wallet
        if asset_id is None:
            wallet = wallet_state_manager.main_wallet
        else:
            wallet = await wallet_state_manager.get_wallet_for_asset_id(asset_id.hex())

        # We need to fill in driver dict entries that we can and raise on discrepencies
        if callable(getattr(wallet, "get_puzzle_info", None)):
            puzzle_driver: PuzzleInfo = await wallet.get_puzzle_info(asset_id)
            if asset_id in driver_dict and driver_dict[asset_id] != puzzle_driver:
                raise ValueError(f"driver_dict specified {driver_dict[asset_id]}, was expecting {puzzle_driver}")
            else:
                driver_dict[asset_id] = puzzle_driver
        elif asset_id is not None:
            raise ValueError(f"Wallet for asset id {asset_id} is not properly integrated for trading")

        # Build the specification for the asset type we want to offer
        asset_types: List[Dict[str, Any]] = []
        if asset_id is not None:
            puzzle_info: PuzzleInfo = driver_dict[asset_id]
            while True:
                type_description: Dict[str, Any] = puzzle_info.info
                if "also" in type_description:
                    del type_description["also"]
                    puzzle_info = puzzle_info.also()
                    asset_types.append(type_description)
                else:
                    asset_types.append(type_description)
                    break

        # We're passing everything in as a dictionary now instead of a single asset_id/amount pair
        offered_asset: Dict[str, Any] = {"with": {"asset_types": asset_types, "amount": str(abs(amount))}, "do": []}

        try:
            if asset_id is not None:
                try:
                    this_solver: Optional[Solver] = solver[asset_id.hex()]
                except KeyError:
                    this_solver = solver["0x" + asset_id.hex()]
            else:
                this_solver = solver[""]
        except KeyError:
            this_solver = None

        # Take note of of the dl dependencies if there are any
        if "dependencies" in this_solver:
            dl_dependencies.append(
                {
                    "type": "require_dl_inclusion",
                    "launcher_ids": ["0x" + dep["launcher_id"].hex() for dep in this_solver["dependencies"]],
                    "values_to_prove": [
                        ["0x" + v.hex() for v in dep["values_to_prove"]] for dep in this_solver["dependencies"]
                    ],
                }
            )

        if wallet.type() == WalletType.DATA_LAYER:
            # Data Layer offers initially were metadata updates, so we shouldn't allow any kind of sending
            assert this_solver is not None
            offered_asset["do"] = [
                [
                    {
                        "type": "update_metadata",
                        # The request used to require "new_root" be in solver so the potential KeyError is good
                        "new_metadata": "(0x" + this_solver["new_root"].hex() + ")",
                    }
                ],
            ]

            additional_actions.append(
                {
                    "with": offered_asset["with"],
                    "do": [
                        MakeAnnouncement("puzzle", Program.to(b"$")).to_solver(),
                    ],
                }
            )
        else:
            action_batch = [
                # This is the parallel to just specifying an amount to offer
                OfferedAmount(abs(amount)).to_solver()
            ]
            # Royalty payments are automatically worked in when you offer fungible assets for an NFT
            if asset_id is None or driver_dict[asset_id].type() != AssetType.SINGLETON.value:
                for payment in calculate_royalty_payments(requested_assets, abs(amount), driver_dict):
                    action_batch.append(OfferedAmount(payment.amount).to_solver())
                    offered_asset["with"]["amount"] = str(
                        cast_to_int(Solver(offered_asset["with"])["amount"]) + payment.amount
                    )

            # The standard XCH should pay the fee
            if asset_id is None and fee > 0:
                action_batch.append(Fee(fee).to_solver())
                offered_asset["with"]["amount"] = str(cast_to_int(Solver(offered_asset["with"])["amount"]) + fee)

            # Provenant NFTs by default clear their ownership on transfer
            elif driver_dict[asset_id].check_type(
                [
                    AssetType.SINGLETON.value,
                    AssetType.METADATA.value,
                    AssetType.OWNERSHIP.value,
                ]
            ):
                action_batch.append(
                    {
                        "type": "update_state",
                        "update": {
                            "new_owner": "()",
                        },
                    }
                )
            offered_asset["do"] = action_batch

        final_solver["actions"].append(offered_asset)

    final_solver["actions"].extend(additional_actions)

    # Make sure the fee gets into the solver
    if None not in offer_dict and fee > 0:
        final_solver["actions"].append(
            {
                "with": {"amount": fee},
                "do": [
                    Fee(fee).to_solver(),
                ],
            }
        )

    # Now lets use the requested items to fill in the bundle dependencies
    final_solver.setdefault("bundle_actions", [])
    final_solver["bundle_actions"].extend(dl_dependencies)
    for asset_id, amount in requested_assets.items():
        if asset_id is None:
            wallet = wallet_state_manager.main_wallet
        else:
            wallet = await wallet_state_manager.get_wallet_for_asset_id(asset_id.hex())

        p2_ph = await wallet_state_manager.main_wallet.get_new_puzzlehash()

        if wallet.type() != WalletType.DATA_LAYER:  # DL singletons are not sent as part of offers by default
            # Asset/amount pairs are assumed to mean requested_payments
            asset_types: List[Solver] = []
            asset_driver = driver_dict[asset_id]
            while True:
                if asset_driver.type() == AssetType.CAT.value:
                    asset_types.append(
                        Solver(
                            {
                                "type": AssetType.CAT.value,
                                "asset_id": asset_driver["tail"],
                            }
                        )
                    )
                elif asset_driver.type() == AssetType.SINGLETON.value:
                    asset_types.append(
                        Solver(
                            {
                                "type": AssetType.SINGLETON.value,
                                "launcher_id": asset_driver["launcher_id"],
                                "launcher_ph": asset_driver["launcher_ph"],
                            }
                        )
                    )
                elif asset_driver.type() == AssetType.METADATA.value:
                    asset_types.append(
                        Solver(
                            {
                                "type": AssetType.METADATA.value,
                                "metadata": asset_driver["metadata"],
                                "metadata_updater_hash": asset_driver["updater_hash"],
                            }
                        )
                    )
                elif asset_driver.type() == AssetType.OWNERSHIP.value:
                    asset_types.append(
                        Solver(
                            {
                                "type": AssetType.OWNERSHIP.value,
                                "owner": asset_driver["owner"],
                                "transfer_program": asset_driver["transfer_program"],
                            }
                        )
                    )

                if asset_driver.also() is None:
                    break
                else:
                    asset_driver = asset_driver.also()

            final_solver["dependencies"].append(
                {
                    "type": "requested_payment",
                    "asset_types": asset_types,
                    "payments": [
                        {
                            "puzhash": "0x" + p2_ph.hex(),
                            "amount": str(amount),
                            "memos": ["0x" + p2_ph.hex()],
                        }
                    ],
                }
            )

        # Also request the royalty payment as a formality
        if asset_id is None or driver_dict[asset_id].type() != AssetType.SINGLETON.value:
            final_solver["dependencies"].extend(
                [
                    {
                        "type": "requested_payment",
                        "asset_id": "0x" + asset_id.hex(),
                        "nonce": "0x" + asset_id.hex(),
                        "payments": [
                            {
                                "puzhash": "0x" + payment.address.hex(),
                                "amount": str(payment.amount),
                                "memos": ["0x" + memo.hex() for memo in payment.memos],
                            }
                        ],
                    }
                    for payment in calculate_royalty_payments(offered_assets, amount, driver_dict)
                ]
            )

    # Finally, we need to special case any stuff that the solver was previously used for
    if "solving_information" not in final_solver:
        final_solver.setdefault("solving_information", [])

    return Solver(final_solver)


def calculate_royalty_payments(
    requested_assets: Dict[Optional[bytes32], int],
    offered_amount: int,
    driver_dict: Dict[bytes32, PuzzleInfo],
) -> List[Payment]:
    """
    Given assets on one side of a trade and an amount being paid for them, return the payments that must be made
    """
    # First, let's take note of all the royalty enabled NFTs
    royalty_nft_assets: List[bytes32] = [
        asset
        for asset in requested_assets
        if asset is not None
        and driver_dict[asset].check_type(  # check if asset is an Royalty Enabled NFT
            [
                AssetType.SINGLETON.value,
                AssetType.METADATA.value,
                AssetType.OWNERSHIP.value,
            ]
        )
    ]

    # Then build what royalty payments we need to make
    royalty_payments: List[Payment] = []
    for asset_id in royalty_nft_assets:
        transfer_info = driver_dict[asset_id].also().also()  # type: ignore
        assert isinstance(transfer_info, PuzzleInfo)
        address: bytes32 = bytes32(transfer_info["transfer_program"]["royalty_address"])
        pts: uint16 = uint16(transfer_info["transfer_program"]["royalty_percentage"])
        extra_royalty_amount = uint64(math.floor(math.floor(offered_amount / len(royalty_nft_assets)) * (pts / 10000)))
        royalty_payments.append(Payment(address, extra_royalty_amount, [address]))

    return royalty_payments


def find_full_prog_from_mod_in_serialized_program(full_program: bytes, start: int, num_curried_args: int) -> Program:
    curried_args: int = 0
    while curried_args < num_curried_args:
        start -= 5
        curried_mod = Program.from_bytes(full_program[start:])
        new_curried_args = list(curried_mod.uncurry()[1].as_iter())
        curried_args += len(new_curried_args)
    if curried_args > num_curried_args:
        raise ValueError(f"Too many curried args: {curried_mod}")
    return curried_mod


def uncurry_to_mod(program: Program, target_mod: Program) -> List[Program]:
    curried_args: List[Program] = []
    while program != target_mod:
        program, new_curried_args = program.uncurry()
        curried_args[:0] = new_curried_args.as_iter()
    return curried_args


def request_payment_to_legacy_encoding(action: RequestPayment, add_nonce: Optional[bytes32] = None) -> CoinSpend:
    puzzle_reveal: Program = OFFER_MOD
    for typ in action.asset_types:
        puzzle_reveal = Program.to(
            [
                2,
                (1, typ["mod"]),
                RequestPayment.build_environment(
                    typ["solution_template"],
                    typ["committed_args"],
                    typ["committed_args"],
                    puzzle_reveal,
                ),
            ]
        )

    dummy_solution: Program = Program.to(
        [
            (
                action.nonce if add_nonce is None or action.nonce is not None else add_nonce,
                [p.as_condition_args() for p in action.payments],
            )
        ]
    )
    return CoinSpend(
        Coin(bytes32([0] * 32), puzzle_reveal.get_tree_hash(), uint64(0)),
        puzzle_reveal,
        dummy_solution,
    )


async def spend_to_offer_bytes(wallet_state_manager: Any, bundle: SpendBundle) -> Offer:
    new_spends: List[CoinSpend] = []
    for spend in bundle.coin_spends:
        # Step 2: Get any wallets that claim to identify the puzzle
        matches: List[Tuple[CoinInfo, List[WalletAction]]] = []
        mod, curried_args = spend.puzzle_reveal.uncurry()
        for wallet in wallet_state_manager.outer_wallets:
            match = await wallet.match_spend(wallet_state_manager, spend, mod, curried_args)
            if match is not None:
                matches.append(match)

        if matches == []:
            continue  # We skip spends we can't identify, if they're important, the spend will fail on chain
        elif len(matches) > 1:
            # QUESTION: Should we support this? Giving multiple interpretations?
            raise ValueError(f"There are multiple ways to describe spend with coin: {spend.coin}")

        # Step 3: Attempt to find matching aliases for the actions
        info, actions, _ = matches[0]
        actions = info.alias_actions(actions, wallet_state_manager.action_aliases)

        # Step 4: Re-order the actions so that DL graftroots are the last applied (need to be outermost)
        dl_graftroot_actions: List[Solver] = []
        all_other_actions: List[Solver] = []
        for action in actions:
            if action.name() == RequireDLInclusion.name():
                dl_graftroot_actions.append(action.to_solver())
                # Step 5: Add the dummy spend that used to encode the requested payment
                for launcher_id in action.launcher_ids:
                    puzzle_reveal = create_host_fullpuz(OFFER_MOD, bytes32([0] * 32), launcher_id)
                    dummy_solution = Program.to([(bytes32([0] * 32), [[bytes32([0] * 32), uint64(1), []]])])
                    new_spends.append(
                        CoinSpend(
                            Coin(bytes32([0] * 32), puzzle_reveal.get_tree_hash(), uint64(0)),
                            puzzle_reveal,
                            dummy_solution,
                        )
                    )
            else:
                if action.name() == RequestPayment.name():
                    # Step 5: Add the dummy spend that used to encode the requested payment
                    new_spends.append(request_payment_to_legacy_encoding(action))
                all_other_actions.append(action.to_solver())

        if len(dl_graftroot_actions) > 1:
            raise ValueError("Legacy offers only support one graftroot for dl inclusions")

        sorted_actions: List[WalletAction] = [*all_other_actions, *dl_graftroot_actions]

        remaining_actions, spend = await info.create_spend_for_actions(
            sorted_actions, wallet_state_manager.action_aliases
        )

        spend_solution: Program = spend.solution.to_program().at("rrf")
        if spend_solution.atom is None and spend_solution.first() == Program.to("graftroot"):
            if dl_graftroot_actions == []:
                new_delegated_solution = Program.to(None)
            else:
                new_delegated_solution = Program.to([None, None, None, None, None])
            spend = dataclasses.replace(
                spend,
                solution=Program.to(
                    [spend.solution.to_program().first(), spend.solution.to_program().at("rf"), new_delegated_solution]
                ),
            )

        if len(remaining_actions) > 0:
            raise ValueError("Attempting to convert the spends to an offer resulted in being unable to spend a coin")
        new_spends.append(spend)

    return bytes(SpendBundle(new_spends, bundle.aggregated_signature))


def legacy_rp_puzzle_to_asset_types(rp_puzzle: Program) -> List[Solver]:
    if rp_puzzle == OFFER_MOD:
        return []

    mod, curried_args = rp_puzzle.uncurry()
    args_list = list(curried_args.as_iter())

    for curried_arg in args_list:
        if curried_arg == OFFER_MOD:
            deeper_asset_types: List[Solver] = []
            break
        inner_mod, _ = curried_arg.uncurry()
        if inner_mod != curried_arg:
            try:
                deeper_asset_types = legacy_rp_puzzle_to_asset_types(curried_arg)
                break
            except ValueError:
                continue
    else:
        raise ValueError("Could not find the offer mod in the requested payments puzzle")

    solution_template: List[str] = ["1" if i != args_list.index(curried_arg) else "0" for i in range(0, len(args_list))]
    solution_template.extend([".", "$"])
    committed_args: List[str] = [disassemble(arg) if arg != curried_arg else "()" for arg in args_list]
    committed_args.extend([".", "()"])
    this_asset_type = Solver(
        {
            "mod": disassemble(mod),
            "solution_template": "(" + solution_template.join(" ") + ")",
            "committed_args": "(" + committed_args.join(" ") + ")",
        }
    )
    return [this_asset_type, *deeper_asset_types]


def offer_to_spend(offer: Offer) -> SpendBundle:
    new_spends: List[CoinSpend] = []
    requested_spends: List[CoinSpend] = [
        cs for cs in offer.to_spend_bundle().coin_spends if cs.coin.parent_coin_info == bytes32([0] * 32)
    ]
    for spend in offer.bundle.coin_spends:
        solution_bytes: bytes = bytes(spend.solution)
        dl_inclusion_index: int = solution_bytes.find(bytes(GRAFTROOT_DL_OFFERS))
        announcement_hash_index: int = -1
        dl_inclusions: List[RequireDLInclusion] = []
        requested_payments: List[RequestPayment] = []
        for requested_spend in requested_spends:
            for announcement in requested_spend.solution.to_program().as_iter():
                announcement_hash: bytes32 = Announcement(
                    requested_spend.puzzle_reveal.get_tree_hash(), announcement.get_tree_hash()
                ).name()
                new_index = solution_bytes.find(announcement_hash)
                if new_index != -1:
                    announcement_hash_index = (
                        new_index if announcement_hash_index == -1 else min(announcement_hash_index, new_index)
                    )
                    asset_types: List[Solver] = legacy_rp_puzzle_to_asset_types(
                        requested_spend.puzzle_reveal.to_program()
                    )
                    nonce: bytes32 = bytes32(announcement.first().as_python())
                    payments: List[Payment] = [
                        Payment.from_condition(Program.to((51, condition)))
                        for condition in announcement.rest().as_iter()
                    ]
                    requested_payments.append(RequestPayment(asset_types, nonce, payments))

        if dl_inclusion_index != -1:
            delegated_puzzle = find_full_prog_from_mod_in_serialized_program(solution_bytes, dl_inclusion_index, 4)
            inner_puzzle, singleton_structs, _, values_to_prove = uncurry_to_mod(delegated_puzzle, GRAFTROOT_DL_OFFERS)
            dl_inclusions.append(
                RequireDLInclusion(
                    [bytes32(struct.at("rf").as_python()) for struct in singleton_structs.as_iter()],
                    [
                        [bytes32(value.as_python()) for value in values.as_iter()]
                        for values in values_to_prove.as_iter()
                    ],
                )
            )
        elif announcement_hash_index != -1:
            delegated_puzzle = Program.from_bytes(solution_bytes[announcement_hash_index - 9 :])
        else:
            new_spends.append(spend)
            continue

        delegated_puzzle_bytes = bytes(delegated_puzzle)
        graftroot_solution_index = solution_bytes.find(delegated_puzzle_bytes) - 3
        graftroot_solution_bytes = solution_bytes[graftroot_solution_index:]
        graftroot_solution = Program.from_bytes(graftroot_solution_bytes)
        delegated_solution = graftroot_solution.at("rrf")

        metadata: Program = Program.to(None)
        for alias in [*requested_payments, *dl_inclusions]:
            graftroot = alias.de_alias()
            metadata = Program.to([graftroot.puzzle_wrapper, graftroot.solution_wrapper, graftroot.metadata]).cons(
                metadata
            )

        inner_delegated_puzzle: Program = delegated_puzzle
        if dl_inclusion_index != -1:
            inner_delegated_puzzle = inner_puzzle
        if announcement_hash_index != -1:
            for _ in requested_payments:
                inner_delegated_puzzle = inner_delegated_puzzle.at("rrfrfr")

        metadata = Program.to("graftroot").cons(Program.to(inner_delegated_puzzle).cons(metadata))

        new_spends.append(
            CoinSpend(spend.coin, spend.puzzle_reveal, solution_for_delegated_puzzle(delegated_puzzle, metadata))
        )

    return SpendBundle(
        new_spends,
        offer.bundle.aggregated_signature,
    )


async def generate_summary_complement(
    wallet_state_manager: Any, summary: Solver, additional_summary: Solver, fee: uint64 = uint64(0)
) -> Solver:
    comp_actions: List[Solver] = []
    comp_bundle_actions: List[Solver] = []
    bundle_actions = summary["bundle_actions"] if "bundle_actions" in summary else []
    paid_fee: bool = fee == 0
    for total_action in [*summary["actions"], *bundle_actions]:
        actions_to_loop = [total_action] if total_action in bundle_actions else total_action["do"]
        for action in actions_to_loop:
            if action["type"] == OfferedAmount.name():
                new_p2_puzhash: bytes32 = await wallet_state_manager.main_wallet.get_new_puzzlehash()
                self_payment: Payment = Payment(new_p2_puzhash, cast_to_int(action["amount"]), [new_p2_puzhash])
                asset_types: List[Solver] = (
                    total_action["with"]["asset_types"] if "asset_types" in total_action["with"] else []
                )
                comp_bundle_actions.append(RequestPayment(asset_types, None, [self_payment]).to_solver())
            elif action["type"] == RequestPayment.name():
                requested_payment = RequestPayment.from_solver(action)
                offered_amount: int = sum(p.amount for p in requested_payment.payments)
                total_amount: int = offered_amount
                if not paid_fee and requested_payment.asset_types == []:
                    total_amount += fee
                    paid_fee = True
                comp_actions.append(
                    Solver(
                        {
                            "with": {"asset_types": requested_payment.asset_types, "amount": str(total_amount)},
                            "do": [
                                OfferedAmount(offered_amount).to_solver(),
                                *(
                                    [Fee(fee).to_solver()]
                                    if not paid_fee and requested_payment.asset_types == []
                                    else []
                                ),
                            ],
                        }
                    )
                )
    return Solver(
        {
            "actions": [
                *comp_actions,
                *([{"with": {"amount": fee}, "do": [Fee(fee).to_solver()]}] if not paid_fee else []),
                *(additional_summary["actions"] if "actions" in additional_summary else []),
            ],
            "bundle_actions": [
                *comp_bundle_actions,
                *(additional_summary["bundle_actions"] if "bundle_actions" in additional_summary else []),
            ],
        }
    )


async def old_solver_to_new(wallet_state_manager: Any, old_solver: Solver) -> Solver:
    actions: List[Solver] = []
    for key, solver in old_solver.info.items():
        try:
            asset_id: bytes32 = bytes32.from_hexstr(key)
        except ValueError:
            continue

        wallet: OuterWallet = await wallet_state_manager.get_wallet_for_asset_id(key)
        if wallet.type() == WalletType.DATA_LAYER:
            asset_types = wallet.get_asset_types(Solver({"launcher_id": "0x" + key if key[0:2] != "0x" else key}))
            actions.append(
                Solver(
                    {
                        "with": {
                            "asset_types": asset_types,
                        },
                        "do": [
                            {
                                "type": "update_metadata",
                                "new_metadata": "(0x" + Solver(solver)["new_root"].hex() + ")",
                            }
                        ],
                    }
                )
            )
            actions.append(
                Solver(
                    {
                        "with": {
                            "asset_types": asset_types,
                        },
                        "do": [MakeAnnouncement("puzzle", Program.to(b"$")).to_solver()],
                    }
                )
            )

    dl_inclusion_proofs: Optional[List[Program]] = []
    if "proofs_of_inclusion" in old_solver:
        for proof in old_solver["proofs_of_inclusion"]:
            dl_inclusion_proofs.append(Program.to((proof[1], proof[2])))

    return Solver(
        {
            "actions": actions,
            "dl_inclusion_proofs": [disassemble(proof) for proof in dl_inclusion_proofs],
            **old_solver.info,
        }
    )


def new_summary_to_old(new_summary: Solver) -> Dict[str, Any]:
    old_summary: Dict[str, Any] = {"offered": [], "requested": []}
    for total_action in new_summary["actions"]:
        asset_description: Dict[str, Any] = total_action["with"].info
        if "amount" in asset_description:
            del asset_description["amount"]
        offered_descriptions: List[Dict[str, Any]] = []
        requested_descriptions: List[Dict[str, Any]] = []
        for action in total_action["do"]:
            if action["type"] == OfferedAmount.name():
                new_offered_descriptions: List[Dict[str, Any]] = []
                added_amount: bool = False
                for description in offered_descriptions:
                    if "amount" in description:
                        new_offered_descriptions.append(
                            {"amount": str(int(description["amount"]) + int(action.info["amount"]))}
                        )
                        added_amount = True
                    else:
                        new_offered_descriptions.append(description)
                offered_descriptions = new_offered_descriptions
                if not added_amount:
                    offered_descriptions.append({"amount": action.info["amount"]})
            elif action["type"] == RequestPayment.name():
                payment_request: RequestPayment = RequestPayment.from_solver(action)
                if len(payment_request.asset_types) > 0:
                    outer_mod: Program = payment_request.asset_types[0]["mod"]
                    if outer_mod == CAT_MOD:
                        asset_id: str = payment_request.asset_types[0]["committed_args"].at("rf").as_python().hex()
                    elif outer_mod == SINGLETON_TOP_LAYER_MOD:
                        asset_id = payment_request.asset_types[0]["committed_args"].at("frf").as_python().hex()

                new_requested_descriptions: List[Dict[str, Any]] = []
                added_amount: bool = False
                for description in requested_descriptions:
                    if "amount" in description:
                        new_requested_descriptions.append(
                            {
                                **({"asset_id": asset_id} if len(payment_request.asset_types) > 0 else {}),
                                "amount": str(
                                    int(description["amount"]) + sum(p.amount for p in payment_request.payments)
                                ),
                            }
                        )
                        added_amount = True
                    else:
                        new_requested_descriptions.append(description)
                requested_descriptions = new_requested_descriptions
                if not added_amount:
                    requested_descriptions.append(
                        {
                            **({"asset_id": asset_id} if len(payment_request.asset_types) > 0 else {}),
                            "amount": str(sum(p.amount for p in payment_request.payments)),
                        }
                    )
            elif action["type"] == UpdateMetadataDL.name():
                offered_descriptions.append({"new_root": action.info["new_metadata"][1:-1]})
            elif action["type"] == RequireDLInclusion.name():
                dl_requirement: RequireDLInclusion = RequireDLInclusion.from_solver(action)
                offered_descriptions.append(
                    {
                        "dependencies": [
                            {"launcher_id": launcher_id.hex(), "values_to_prove": [v.hex() for v in values]}
                            for launcher_id, values in zip(dl_requirement.launcher_ids, dl_requirement.values_to_prove)
                        ]
                    }
                )

        offered: Dict[str, Any] = asset_description.copy()
        requested: Dict[str, Any] = asset_description.copy()
        for description in offered_descriptions:
            offered.update(description)
        for description in requested_descriptions:
            requested.update(description)

        if offered_descriptions != []:
            old_summary["offered"].append(offered)
        if requested_descriptions != []:
            old_summary["requested"].append(requested)

    return ast.literal_eval(repr(old_summary))