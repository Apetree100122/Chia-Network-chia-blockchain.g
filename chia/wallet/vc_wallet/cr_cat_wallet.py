from __future__ import annotations

import dataclasses
import logging
import time
import traceback
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from blspy import G1Element, G2Element

from chia.server.ws_connection import WSChiaConnection
from chia.types.announcement import Announcement
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.types.spend_bundle import SpendBundle
from chia.util.byte_types import hexstr_to_bytes
from chia.util.hash import std_hash
from chia.util.ints import uint32, uint64
from chia.wallet.cat_wallet.cat_info import CRCATInfo
from chia.wallet.cat_wallet.cat_wallet import CATWallet
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.outer_puzzles import AssetType
from chia.wallet.payment import Payment
from chia.wallet.puzzle_drivers import PuzzleInfo
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.util.compute_memos import compute_memos
from chia.wallet.util.transaction_type import TransactionType
from chia.wallet.util.wallet_sync_utils import fetch_coin_spend_for_coin_state
from chia.wallet.util.wallet_types import WalletType
from chia.wallet.vc_wallet.cr_cat_drivers import CRCAT, ProofsChecker
from chia.wallet.vc_wallet.cr_cat_store import CRCATStore
from chia.wallet.vc_wallet.vc_drivers import VerifiedCredential
from chia.wallet.vc_wallet.vc_wallet import VCWallet
from chia.wallet.wallet import Wallet
from chia.wallet.wallet_info import WalletInfo

if TYPE_CHECKING:
    from chia.wallet.wallet_state_manager import WalletStateManager


class CRCATWallet(CATWallet):
    wallet_state_manager: WalletStateManager
    log: logging.Logger
    wallet_info: WalletInfo
    info: CRCATInfo
    standard_wallet: Wallet
    cost_of_single_tx: int
    store: CRCATStore
    authorized_providers: List[bytes32]
    proofs_checker: ProofsChecker

    @staticmethod
    def default_wallet_name_for_unknown_cat(limitations_program_hash_hex: str) -> str:
        return f"CAT {limitations_program_hash_hex[:16]}..."

    @staticmethod
    async def create_new_cat_wallet(
        wallet_state_manager: WalletStateManager,
        wallet: Wallet,
        cat_tail_info: Dict[str, Any],
        amount: uint64,
        name: Optional[str] = None,
    ) -> "CATWallet":
        raise NotImplementedError("create_new_cat_wallet is a legacy method and is not available on CR-CAT wallets")

    @staticmethod
    async def get_or_create_wallet_for_cat(
        wallet_state_manager: WalletStateManager,
        wallet: Wallet,
        limitations_program_hash_hex: str,
        name: Optional[str] = None,
        authorized_providers: Optional[List[bytes32]] = None,
        proofs_checker: Optional[ProofsChecker] = None,
    ) -> CRCATWallet:
        if authorized_providers is None or proofs_checker is None:
            raise ValueError("get_or_create_wallet_for_cat was call on CRCATWallet without proper arguments")
        self = CRCATWallet()
        self.cost_of_single_tx = 78000000  # Measured in testing
        self.standard_wallet = wallet
        if name is None:
            name = self.default_wallet_name_for_unknown_cat(limitations_program_hash_hex)
        self.log = logging.getLogger(name)

        tail_hash = bytes32.from_hexstr(limitations_program_hash_hex)

        for id, w in wallet_state_manager.wallets.items():
            if w.type() == CRCATWallet.type():
                assert isinstance(w, CRCATWallet)
                if w.get_asset_id() == limitations_program_hash_hex:
                    self.log.warning("Not creating wallet for already existing CR-CAT wallet")
                    return w

        self.wallet_state_manager = wallet_state_manager
        self.authorized_providers = authorized_providers
        self.proofs_checker = proofs_checker

        self.info = CRCATInfo(tail_hash, None, authorized_providers, proofs_checker)
        info_as_string = bytes(self.info).hex()
        self.wallet_info = await wallet_state_manager.user_store.create_wallet(name, WalletType.CRCAT, info_as_string)
        self.store = self.wallet_state_manager.cr_cat_store

        await self.wallet_state_manager.add_new_wallet(self)
        return self

    @classmethod
    async def create_from_puzzle_info(
        cls,
        wallet_state_manager: WalletStateManager,
        wallet: Wallet,
        puzzle_driver: PuzzleInfo,
        name: Optional[str] = None,
        # We're hinting this as Any for mypy by should explore adding this to the wallet protocol and hinting properly
        potential_subclasses: Dict[AssetType, Any] = {},
    ) -> Any:
        cr_layer: Optional[PuzzleInfo] = puzzle_driver.also()
        if cr_layer is None:
            raise ValueError("create_from_puzzle_info called on CRCATWallet with a non CR-CAT puzzle driver")
        return await cls.get_or_create_wallet_for_cat(
            wallet_state_manager,
            wallet,
            puzzle_driver["tail"].hex(),
            name,
            [bytes32(provider) for provider in cr_layer["authorized_providers"]],
            ProofsChecker.from_program(cr_layer["proofs_checker"]),
        )

    @staticmethod
    async def create(
        wallet_state_manager: WalletStateManager,
        wallet: Wallet,
        wallet_info: WalletInfo,
    ) -> CRCATWallet:
        self = CRCATWallet()

        self.log = logging.getLogger(__name__)
        self.cost_of_single_tx = 78000000
        self.wallet_state_manager = wallet_state_manager
        self.wallet_info = wallet_info
        self.standard_wallet = wallet
        self.info = CRCATInfo.from_bytes(hexstr_to_bytes(self.wallet_info.data))
        self.store = self.wallet_state_manager.cr_cat_store
        self.authorized_providers = self.info.authorized_providers
        self.proofs_checker = self.info.proofs_checker
        return self

    @classmethod
    def type(cls) -> WalletType:
        return WalletType.CRCAT

    def id(self) -> uint32:
        return self.wallet_info.id

    def get_asset_id(self) -> str:
        return self.info.limitations_program_hash.hex()

    async def set_tail_program(self, tail_program: str) -> None:
        raise NotImplementedError("set_tail_program is a legacy method and is not available on CR-CAT wallets")

    async def coin_added(self, coin: Coin, height: uint32, peer: WSChiaConnection) -> None:
        """Notification from wallet state manager that wallet has been received."""
        self.log.info(f"CR-CAT wallet has been notified that {coin.name().hex()} was added")
        try:
            coin_state = await self.wallet_state_manager.wallet_node.get_coin_state([coin.parent_coin_info], peer=peer)
            coin_spend = await fetch_coin_spend_for_coin_state(coin_state[0], peer)
            await self.puzzle_solution_received(coin_spend, coin)
        except Exception as e:
            self.log.debug(f"Exception: {e}, traceback: {traceback.format_exc()}")

    async def puzzle_solution_received(self, coin_spend: CoinSpend, coin: Coin) -> None:
        try:
            new_cr_cats: List[CRCAT] = CRCAT.get_next_from_coin_spend(coin_spend)
            cr_cat: CRCAT = list(filter(lambda c: c.coin.name() == coin.name(), new_cr_cats))[0]
            await self.store.add_or_replace_crcat(cr_cat)
        except Exception:
            # The parent is not a CAT which means we need to scrub all of its children from our DB
            child_coin_records = await self.wallet_state_manager.coin_store.get_coin_records_by_parent_id(
                coin_spend.coin.name()
            )
            if len(child_coin_records) > 0:
                for record in child_coin_records:
                    if record.wallet_id == self.id():
                        await self.wallet_state_manager.coin_store.delete_coin_record(record.coin.name())
                        # We also need to make sure there's no record of the transaction
                        await self.wallet_state_manager.tx_store.delete_transaction_record(record.coin.name())

    def require_derivation_paths(self) -> bool:
        return False

    def puzzle_for_pk(self, pubkey: G1Element) -> Program:
        raise NotImplementedError("puzzle_for_pk is a legacy method and is not available on CR-CAT wallets")

    def puzzle_hash_for_pk(self, pubkey: G1Element) -> bytes32:
        raise NotImplementedError("puzzle_hash_for_pk is a legacy method and is not available on CR-CAT wallets")

    async def get_new_cat_puzzle_hash(self) -> bytes32:
        raise NotImplementedError("get_new_cat_puzzle_hash is a legacy method and is not available on CR-CAT wallets")

    async def sign(self, spend_bundle: SpendBundle) -> SpendBundle:
        raise NotImplementedError("get_new_cat_puzzle_hash is a legacy method and is not available on CR-CAT wallets")

    async def inner_puzzle_for_cat_puzhash(self, cat_hash: bytes32) -> Program:
        raise NotImplementedError(
            "inner_puzzle_for_cat_puzhash is a legacy method and is not available on CR-CAT wallets"
        )

    async def convert_puzzle_hash(self, puzzle_hash: bytes32) -> bytes32:
        return puzzle_hash

    async def get_lineage_proof_for_coin(self, coin: Coin) -> Optional[LineageProof]:
        potential_cr_cat: Optional[CRCAT] = await self.store.get_crcat(
            coin.name(), self.authorized_providers, self.proofs_checker
        )
        if potential_cr_cat is None:
            return None
        else:
            return potential_cr_cat.lineage_proof

    async def _generate_unsigned_spendbundle(
        self,
        payments: List[Payment],
        fee: uint64 = uint64(0),
        cat_discrepancy: Optional[Tuple[int, Program, Program]] = None,  # (extra_delta, tail_reveal, tail_solution)
        coins: Optional[Set[Coin]] = None,
        coin_announcements_to_consume: Optional[Set[Announcement]] = None,
        puzzle_announcements_to_consume: Optional[Set[Announcement]] = None,
        min_coin_amount: Optional[uint64] = None,
        max_coin_amount: Optional[uint64] = None,
        exclude_coin_amounts: Optional[List[uint64]] = None,
        exclude_coins: Optional[Set[Coin]] = None,
        reuse_puzhash: Optional[bool] = None,
    ) -> Tuple[SpendBundle, List[TransactionRecord]]:
        if coin_announcements_to_consume is not None:
            coin_announcements_bytes: Optional[Set[bytes32]] = {a.name() for a in coin_announcements_to_consume}
        else:
            coin_announcements_bytes = None

        if puzzle_announcements_to_consume is not None:
            puzzle_announcements_bytes: Optional[Set[bytes32]] = {a.name() for a in puzzle_announcements_to_consume}
        else:
            puzzle_announcements_bytes = None

        if cat_discrepancy is not None:
            extra_delta, tail_reveal, tail_solution = cat_discrepancy
        else:
            extra_delta, tail_reveal, tail_solution = 0, Program.to([]), Program.to([])
        payment_amount: int = sum([p.amount for p in payments])
        starting_amount: int = payment_amount - extra_delta
        if reuse_puzhash is None:
            reuse_puzhash_config = self.wallet_state_manager.config.get("reuse_public_key_for_change", None)
            if reuse_puzhash_config is None:
                reuse_puzhash = False
            else:
                reuse_puzhash = reuse_puzhash_config.get(
                    str(self.wallet_state_manager.wallet_node.logged_in_fingerprint), False
                )
        if coins is None:
            if exclude_coins is None:
                exclude_coins = set()
            cat_coins = await self.select_coins(
                uint64(starting_amount),
                exclude=list(exclude_coins),
                min_coin_amount=min_coin_amount,
                max_coin_amount=max_coin_amount,
                excluded_coin_amounts=exclude_coin_amounts,
            )
        elif exclude_coins is not None:
            raise ValueError("Can't exclude coins when also specifically including coins")
        else:
            cat_coins = coins

        selected_cat_amount = sum([c.amount for c in cat_coins])
        assert selected_cat_amount >= starting_amount

        # Figure out if we need to absorb/melt some XCH as part of this
        regular_chia_to_claim: int = 0
        if payment_amount > starting_amount:
            fee = uint64(fee + payment_amount - starting_amount)
        elif payment_amount < starting_amount:
            regular_chia_to_claim = payment_amount

        need_chia_transaction = (fee > 0 or regular_chia_to_claim > 0) and (fee - regular_chia_to_claim != 0)

        # Calculate standard puzzle solutions
        change = selected_cat_amount - starting_amount
        primaries: List[Payment] = []
        for payment in payments:
            primaries.append(payment)

        if change > 0:
            derivation_record = await self.wallet_state_manager.puzzle_store.get_derivation_record_for_puzzle_hash(
                list(cat_coins)[0].puzzle_hash
            )
            if derivation_record is not None and reuse_puzhash:
                change_puzhash = self.standard_wallet.puzzle_hash_for_pk(derivation_record.pubkey)
                for payment in payments:
                    if change_puzhash == payment.puzzle_hash and change == payment.amount:
                        # We cannot create two coins has same id, create a new puzhash for the change
                        change_puzhash = await self.get_new_inner_hash()
                        break
            else:
                change_puzhash = await self.get_new_inner_hash()
            primaries.append(Payment(change_puzhash, uint64(change), []))

        # Find the VC Wallet
        vc_wallet: VCWallet
        for wallet in self.wallet_state_manager.wallets.values():
            if WalletType(wallet.type()) == WalletType.VC:
                assert isinstance(wallet, VCWallet)
                vc_wallet = wallet
                break
        else:
            raise RuntimeError("CR-CATs cannot be spent without an appropriate VC")

        # Loop through the coins we've selected and gather the information we need to spend them
        vc: Optional[VerifiedCredential] = None
        vc_announcements_to_make: List[bytes] = []
        inner_spends: List[Tuple[CRCAT, Program, Program]] = []
        chia_tx = None
        first = True
        announcement: Announcement
        for coin in cat_coins:
            if vc is None:
                vc = await vc_wallet.get_vc_with_provider_in(self.authorized_providers)
            crcat: Optional[CRCAT] = await self.store.get_crcat(
                coin.name(), self.authorized_providers, self.proofs_checker
            )
            if crcat is None:
                raise RuntimeError(f"Attempting to spend a coin that we have not synced as a CR-CAT: {coin}")
            vc_announcements_to_make.append(crcat.expected_announcement())
            if first:
                first = False
                announcement = Announcement(coin.name(), std_hash(b"".join([c.name() for c in cat_coins])))
                if need_chia_transaction:
                    if fee > regular_chia_to_claim:
                        chia_tx, _ = await self.create_tandem_xch_tx(
                            fee,
                            uint64(regular_chia_to_claim),
                            announcement_to_assert=announcement,
                            min_coin_amount=min_coin_amount,
                            max_coin_amount=max_coin_amount,
                            exclude_coin_amounts=exclude_coin_amounts,
                            reuse_puzhash=reuse_puzhash,
                        )
                        innersol = self.standard_wallet.make_solution(
                            primaries=primaries,
                            coin_announcements={announcement.message},
                            coin_announcements_to_assert=coin_announcements_bytes,
                            puzzle_announcements_to_assert=puzzle_announcements_bytes,
                        )
                    elif regular_chia_to_claim > fee:
                        chia_tx, _ = await self.create_tandem_xch_tx(
                            fee,
                            uint64(regular_chia_to_claim),
                            min_coin_amount=min_coin_amount,
                            max_coin_amount=max_coin_amount,
                            exclude_coin_amounts=exclude_coin_amounts,
                            reuse_puzhash=reuse_puzhash,
                        )
                        innersol = self.standard_wallet.make_solution(
                            primaries=primaries,
                            coin_announcements={announcement.message},
                            coin_announcements_to_assert={announcement.name()},
                        )
                else:
                    innersol = self.standard_wallet.make_solution(
                        primaries=primaries,
                        coin_announcements={announcement.message},
                        coin_announcements_to_assert=coin_announcements_bytes,
                        puzzle_announcements_to_assert=puzzle_announcements_bytes,
                    )
            else:
                innersol = self.standard_wallet.make_solution(
                    primaries=[],
                    coin_announcements_to_assert={announcement.name()},
                )
            if cat_discrepancy is not None:
                # TODO: This line is a hack, make_solution should allow us to pass extra conditions to it
                innersol = Program.to(
                    [[], (1, Program.to([51, None, -113, tail_reveal, tail_solution]).cons(innersol.at("rfr"))), []]
                )
            inner_derivation_record = (
                await self.wallet_state_manager.puzzle_store.get_derivation_record_for_puzzle_hash(
                    crcat.inner_puzzle_hash
                )
            )
            if inner_derivation_record is None:
                raise RuntimeError(
                    f"CR-CAT {crcat} has an inner puzzle hash {crcat.inner_puzzle_hash} that we don't have the keys for"
                )
            inner_puzzle: Program = self.standard_wallet.puzzle_for_pk(inner_derivation_record.pubkey)
            inner_spends.append(
                (
                    crcat,
                    inner_puzzle,
                    innersol,
                )
            )

        if vc is None:
            raise RuntimeError("Spending no cat coins is not an appropriate use of _generate_unsigned_spendbundle")
        if vc.proof_hash is None:
            raise RuntimeError("CR-CATs found an appropriate VC but that VC contains no proofs")

        proof_of_inclusions: Program = await vc_wallet.proof_of_inclusions_for_root_and_keys(
            vc.proof_hash, self.proofs_checker.flags
        )

        expected_announcements, coin_spends, _ = CRCAT.spend_many(
            inner_spends,
            proof_of_inclusions,
            Program.to(None),  # TODO: With more proofs checkers, this may need to be flexible. For now, it's hardcoded.
            vc.proof_provider,
            vc.launcher_id,
            vc.wrap_inner_with_backdoor().get_tree_hash(),
        )
        vc_txs: List[TransactionRecord] = await vc_wallet.generate_signed_transaction(
            vc.launcher_id,
            puzzle_announcements=set(vc_announcements_to_make),
            coin_announcements_to_consume=set((*expected_announcements, announcement)),
            reuse_puzhash=reuse_puzhash,
        )

        return (
            SpendBundle(
                [
                    *coin_spends,
                    *(
                        spend
                        for tx in vc_txs
                        for spend in tx.spend_bundle.coin_spends  # type: ignore [union-attr]
                        if tx.spend_bundle is not None
                    ),
                    *(
                        spend
                        for spend in chia_tx.spend_bundle.coin_spends  # type: ignore [union-attr]
                        if chia_tx is not None
                    ),
                ],
                G2Element(),
            ),
            [*vc_txs, *([chia_tx] if chia_tx is not None else [])],
        )

    async def generate_signed_transaction(
        self,
        amounts: List[uint64],
        puzzle_hashes: List[bytes32],
        fee: uint64 = uint64(0),
        coins: Optional[Set[Coin]] = None,
        ignore_max_send_amount: bool = False,
        memos: Optional[List[List[bytes]]] = None,
        coin_announcements_to_consume: Optional[Set[Announcement]] = None,
        puzzle_announcements_to_consume: Optional[Set[Announcement]] = None,
        min_coin_amount: Optional[uint64] = None,
        max_coin_amount: Optional[uint64] = None,
        exclude_coin_amounts: Optional[List[uint64]] = None,
        exclude_cat_coins: Optional[Set[Coin]] = None,
        cat_discrepancy: Optional[Tuple[int, Program, Program]] = None,  # (extra_delta, tail_reveal, tail_solution)
        reuse_puzhash: Optional[bool] = None,
    ) -> List[TransactionRecord]:
        if memos is None:
            memos = [[] for _ in range(len(puzzle_hashes))]

        if not (len(memos) == len(puzzle_hashes) == len(amounts)):
            raise ValueError("Memos, puzzle_hashes, and amounts must have the same length")

        payments = []
        for amount, puzhash, memo_list in zip(amounts, puzzle_hashes, memos):
            memos_with_hint: List[bytes] = [puzhash]
            memos_with_hint.extend(memo_list)
            payments.append(Payment(puzhash, amount, memos_with_hint))

        payment_sum = sum([p.amount for p in payments])
        if not ignore_max_send_amount:
            max_send = await self.get_max_send_amount()
            if payment_sum > max_send:
                raise ValueError(f"Can't send more than {max_send} mojos in a single transaction")
        unsigned_spend_bundle, other_txs = await self._generate_unsigned_spendbundle(
            payments,
            fee,
            cat_discrepancy=cat_discrepancy,  # (extra_delta, tail_reveal, tail_solution)
            coins=coins,
            coin_announcements_to_consume=coin_announcements_to_consume,
            puzzle_announcements_to_consume=puzzle_announcements_to_consume,
            min_coin_amount=min_coin_amount,
            max_coin_amount=max_coin_amount,
            exclude_coin_amounts=exclude_coin_amounts,
            exclude_coins=exclude_cat_coins,
            reuse_puzhash=reuse_puzhash,
        )

        signed_spend_bundle: SpendBundle = await self.wallet_state_manager.main_wallet.sign_transaction(
            unsigned_spend_bundle.coin_spends
        )

        tx_list = [
            TransactionRecord(
                confirmed_at_height=uint32(0),
                created_at_time=uint64(int(time.time())),
                to_puzzle_hash=payment.puzzle_hash,
                amount=payment.amount,
                fee_amount=fee,
                confirmed=False,
                sent=uint32(0),
                spend_bundle=signed_spend_bundle if i == 0 else None,
                additions=signed_spend_bundle.additions() if i == 0 else [],
                removals=signed_spend_bundle.removals() if i == 0 else [],
                wallet_id=self.id(),
                sent_to=[],
                trade_id=None,
                type=uint32(TransactionType.OUTGOING_TX.value),
                name=signed_spend_bundle.name(),
                memos=list(compute_memos(signed_spend_bundle).items()),
            )
            for i, payment in enumerate(payments)
        ]

        return [*tx_list, *(dataclasses.replace(tx, spend_bundle=None) for tx in other_txs)]

    async def match_puzzle_info(self, puzzle_driver: PuzzleInfo) -> bool:
        if (
            AssetType(puzzle_driver.type()) == AssetType.CAT
            and puzzle_driver["tail"] == self.info.limitations_program_hash
        ):
            inner_puzzle_driver: Optional[PuzzleInfo] = puzzle_driver.also()
            if inner_puzzle_driver is None:
                raise ValueError("Malformed puzzle driver passed to CRCATWallet.match_puzzle_info")
            return (
                AssetType(inner_puzzle_driver.type()) == AssetType.CR
                and [bytes32(provider) for provider in inner_puzzle_driver["authorized_providers"]]
                == self.info.authorized_providers
                and ProofsChecker.from_program(inner_puzzle_driver["proofs_checker"]) == self.info.proofs_checker
            )
        return False

    async def get_puzzle_info(self, asset_id: bytes32) -> PuzzleInfo:
        return PuzzleInfo(
            {
                "type": AssetType.CAT.value,
                "tail": "0x" + self.info.limitations_program_hash.hex(),
                "also": {
                    "type": AssetType.CR.value,
                    "authorized_providers": ["0x" + provider.hex() for provider in self.info.authorized_providers],
                    "proofs_checker": self.info.proofs_checker.as_program(),
                },
            }
        )


if TYPE_CHECKING:
    from chia.wallet.wallet_protocol import WalletProtocol

    _dummy: WalletProtocol = CRCATWallet()
