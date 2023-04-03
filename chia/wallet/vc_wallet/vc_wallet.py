from __future__ import annotations

import dataclasses
import logging
import time
from typing import TYPE_CHECKING, Any, List, Optional, Set, Tuple, Type, TypeVar

from blspy import G1Element
from chia_rs.chia_rs import CoinState

from chia.server.ws_connection import WSChiaConnection
from chia.types.announcement import Announcement
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.types.spend_bundle import SpendBundle
from chia.util.ints import uint32, uint64, uint128
from chia.wallet.did_wallet.did_wallet import DIDWallet
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import solution_for_conditions
from chia.wallet.sign_coin_spends import sign_coin_spends
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.util.compute_memos import compute_memos
from chia.wallet.util.transaction_type import TransactionType
from chia.wallet.util.wallet_types import AmountWithPuzzlehash, WalletType
from chia.wallet.vc_wallet.vc_drivers import VerifiedCredential
from chia.wallet.vc_wallet.vc_store import VCRecord, VCStore
from chia.wallet.wallet import Wallet
from chia.wallet.wallet_coin_record import WalletCoinRecord
from chia.wallet.wallet_info import WalletInfo

_T_VCWallet = TypeVar("_T_VCWallet", bound="VCWallet")


class VCWallet:
    # WalletStateManager is only imported for type hinting thus leaving pylint
    # unable to process this
    wallet_state_manager: Any  # pylint: disable=used-before-assignment
    log: logging.Logger
    standard_wallet: Wallet
    wallet_info: WalletInfo
    store: VCStore

    @classmethod
    async def create_new_vc_wallet(
        cls: Type[_T_VCWallet],
        wallet_state_manager: Any,
        wallet: Wallet,
        name: Optional[str] = None,
    ) -> _T_VCWallet:
        self = cls()
        self.wallet_state_manager = wallet_state_manager
        self.standard_wallet = wallet
        name = "VCWallet" if name is None else name
        self.log = logging.getLogger(name if name else __name__)
        self.store = wallet_state_manager.vc_store
        self.wallet_info = await wallet_state_manager.user_store.create_wallet(name, uint32(WalletType.VC.value), "")
        await self.wallet_state_manager.add_new_wallet(self, False)
        return self

    @classmethod
    async def create(
        cls: Type[_T_VCWallet],
        wallet_state_manager: Any,
        wallet: Wallet,
        wallet_info: WalletInfo,
        name: Optional[str] = None,
    ) -> _T_VCWallet:
        self = cls()
        self.wallet_state_manager = wallet_state_manager
        self.standard_wallet = wallet
        self.log = logging.getLogger(name if name else wallet_info.name)
        self.wallet_info = wallet_info
        self.store = wallet_state_manager.vc_store
        return self

    @classmethod
    def type(cls) -> WalletType:
        return WalletType.VC

    def id(self) -> uint32:
        return self.wallet_info.id

    async def coin_added(self, coin: Coin, height: uint32, peer: WSChiaConnection) -> None:
        """
        An unspent coin has arrived to our wallet. Get the parent spend to construct the current VerifiedCredential
        representation of the coin and add it to the DB if it's the newest version of the singleton.
        """
        wallet_node = self.wallet_state_manager.wallet_node
        coin_states: Optional[List[CoinState]] = await wallet_node.get_coin_state([coin.parent_coin_info], peer=peer)
        if coin_states is None:
            self.log.error(f"Cannot find parent coin of the verified credential coin: {coin.name().hex()}")
            return
        parent_coin = coin_states[0].coin
        cs = await wallet_node.fetch_puzzle_solution(height, parent_coin, peer)
        if cs is None:
            self.log.error(f"Cannot get verified credential coin: {coin.name().hex()} puzzle and solution")
            return
        vc = VerifiedCredential.get_next_from_coin_spend(cs)
        vc_record: VCRecord = VCRecord(vc, height)
        await self.store.add_or_replace_vc_record(vc_record)

    async def remove_coin(self, coin: Coin, height: uint32) -> None:
        """
        remove the VC if it is transferred to another key
        :param coin:
        :param height:
        :return:
        """
        vc_record: Optional[VCRecord] = await self.store.get_vc_record_by_coin_id(coin.name())
        if vc_record is not None:
            await self.store.delete_vc_record(vc_record.vc.launcher_id)

    async def get_vc_record_for_launcher_id(self, launcher_id: bytes32) -> VCRecord:
        """
        Go into the store and get the VC Record representing the latest representation of the VC we have on chain.
        """
        vc_record = await self.store.get_vc_record(launcher_id)
        if vc_record is None:
            raise ValueError(f"Verified credential {launcher_id.hex()} doesn't exist.")
        return vc_record

    async def launch_new_vc(
        self,
        provider_did: bytes32,
        inner_puzzle_hash: Optional[bytes32] = None,
        fee: uint64 = uint64(0),
    ) -> Tuple[VCRecord, List[TransactionRecord]]:
        """
        Given the DID ID of a proof provider, mint a brand new VC with an empty slot for proofs.

        Returns the tx records associated with the transaction as well as the expected unconfirmed VCRecord.
        """
        # Check if we own the DID
        found_did = False
        for _, wallet in self.wallet_state_manager.wallets.items():
            if wallet.type() == WalletType.DECENTRALIZED_ID:
                assert isinstance(wallet, DIDWallet)
                if bytes32.fromhex(wallet.get_my_DID()) == provider_did:
                    found_did = True
                    break
        if not found_did:
            raise ValueError(f"You don't own the DID {provider_did.hex()}")
        # Mint VC
        coins = await self.standard_wallet.select_coins(uint64(2 + fee), min_coin_amount=uint64(2 + fee))
        if len(coins) == 0:
            raise ValueError("Cannot find a coin to mint the verified credential.")
        if inner_puzzle_hash is None:
            inner_puzzle_hash = await self.standard_wallet.get_puzzle_hash(new=False)
        original_coin = coins.copy().pop()
        dpuz, coin_spends, vc = VerifiedCredential.launch(
            original_coin,
            provider_did,
            inner_puzzle_hash,
            [inner_puzzle_hash],
        )
        solution = solution_for_conditions(dpuz.rest())
        original_puzzle = await self.standard_wallet.puzzle_for_puzzle_hash(original_coin.puzzle_hash)
        coin_spends.append(CoinSpend(original_coin, original_puzzle, solution))
        spend_bundle = await sign_coin_spends(
            coin_spends,
            self.standard_wallet.secret_key_store.secret_key_for_public_key,
            self.wallet_state_manager.constants.AGG_SIG_ME_ADDITIONAL_DATA,
            self.wallet_state_manager.constants.MAX_BLOCK_COST_CLVM,
        )
        now = uint64(int(time.time()))
        add_list: List[Coin] = list(spend_bundle.additions())
        rem_list: List[Coin] = list(spend_bundle.removals())
        vc_record: VCRecord = VCRecord(vc, uint32(0))
        tx = TransactionRecord(
            confirmed_at_height=uint32(0),
            created_at_time=now,
            to_puzzle_hash=inner_puzzle_hash,
            amount=uint64(1),
            fee_amount=uint64(fee),
            confirmed=False,
            sent=uint32(0),
            spend_bundle=spend_bundle,
            additions=add_list,
            removals=rem_list,
            wallet_id=uint32(1),
            sent_to=[],
            trade_id=None,
            type=uint32(TransactionType.OUTGOING_TX.value),
            name=spend_bundle.name(),
            memos=list(compute_memos(spend_bundle).items()),
        )

        return vc_record, [tx]

    async def generate_signed_transaction(
        self,
        vc_id: bytes32,
        fee: uint64 = uint64(0),
        new_inner_puzhash: Optional[bytes32] = None,
        coin_announcements_to_consume: Optional[Set[Announcement]] = None,
        puzzle_announcements_to_consume: Optional[Set[Announcement]] = None,
        new_proof_hash: Optional[bytes32] = None,  # Requires that this key posesses the DID to update the specified VC
        provider_inner_puzhash: Optional[bytes32] = None,
        reuse_puzhash: Optional[bool] = None,
    ) -> List[TransactionRecord]:
        """
        Entry point for two standard actions:
         - Cycle the singleton and make an announcement authorizing something
         - Update the hash of the proofs contained within the VC (new_proof_hash is not None)

        Returns a 1 - 3 TransactionRecord objects depending on whether or not there's a fee and whether or not there's
        a DID announcement involved.
        """
        # Find verified credential
        vc_record = await self.get_vc_record_for_launcher_id(vc_id)
        if vc_record.confirmed_at_height == 0:
            raise ValueError(f"Verified credential {vc_id.hex()} is not confirmed, please try again later.")
        inner_puzhash: bytes32 = vc_record.vc.inner_puzzle_hash
        inner_puzzle: Program = await self.standard_wallet.puzzle_for_puzzle_hash(inner_puzhash)
        if new_inner_puzhash is None:
            new_inner_puzhash = inner_puzhash
        if coin_announcements_to_consume is not None:
            coin_announcements_bytes: Optional[Set[bytes32]] = {a.name() for a in coin_announcements_to_consume}
        else:
            coin_announcements_bytes = None

        if puzzle_announcements_to_consume is not None:
            puzzle_announcements_bytes: Optional[Set[bytes32]] = {a.name() for a in puzzle_announcements_to_consume}
        else:
            puzzle_announcements_bytes = None

        primaries: List[AmountWithPuzzlehash] = [
            {"puzzlehash": new_inner_puzhash, "amount": uint64(vc_record.vc.coin.amount), "memos": [new_inner_puzhash]}
        ]

        if fee > 0:
            announcement_to_make = vc_record.vc.coin.name()
            chia_tx = await self.create_tandem_xch_tx(
                fee, Announcement(vc_record.vc.coin.name(), announcement_to_make), reuse_puzhash=reuse_puzhash
            )
        else:
            announcement_to_make = None
            chia_tx = None
        if new_proof_hash is not None:
            if provider_inner_puzhash is None:
                raise ValueError(f"Provider inner puzzle hash is required for update VC {vc_id.hex()} proof.")
            magic_condition = vc_record.vc.magic_condition_for_new_proofs(new_proof_hash, provider_inner_puzhash)
        else:
            magic_condition = vc_record.vc.standard_magic_condition()
        innersol: Program = self.standard_wallet.make_solution(
            primaries=primaries,
            coin_announcements=None if announcement_to_make is None else set((announcement_to_make,)),
            coin_announcements_to_assert=coin_announcements_bytes,
            puzzle_announcements_to_assert=puzzle_announcements_bytes,
            magic_conditions=[magic_condition],
        )
        did_announcement, coin_spend, vc = vc_record.vc.do_spend(inner_puzzle, innersol, new_proof_hash)
        spend_bundles = [
            await sign_coin_spends(
                [coin_spend],
                self.standard_wallet.secret_key_store.secret_key_for_public_key,
                self.wallet_state_manager.constants.AGG_SIG_ME_ADDITIONAL_DATA,
                self.wallet_state_manager.constants.MAX_BLOCK_COST_CLVM,
            )
        ]
        if did_announcement is not None:
            # Need to spend DID
            for _, wallet in self.wallet_state_manager.wallets.items():
                if wallet.type() == WalletType.DECENTRALIZED_ID:
                    assert isinstance(wallet, DIDWallet)
                    if bytes32.fromhex(wallet.get_my_DID()) == vc_record.vc.proof_provider:
                        self.log.debug("Creating announcement from DID for vc: %s", vc_id.hex())
                        did_bundle = await wallet.create_message_spend(puzzle_announcements={bytes(did_announcement)})
                        spend_bundles.append(did_bundle)
                        break
            else:
                raise ValueError(f"Cannot find the required DID {vc_record.vc.proof_provider.hex()}.")
        tx_list: List[TransactionRecord] = []
        if chia_tx is not None and chia_tx.spend_bundle is not None:
            spend_bundles.append(chia_tx.spend_bundle)
            tx_list.append(dataclasses.replace(chia_tx, spend_bundle=None))
        spend_bundle = SpendBundle.aggregate(spend_bundles)
        now = uint64(int(time.time()))
        add_list: List[Coin] = list(spend_bundle.additions())
        rem_list: List[Coin] = list(spend_bundle.removals())
        tx_list.append(
            TransactionRecord(
                confirmed_at_height=uint32(0),
                created_at_time=now,
                to_puzzle_hash=new_inner_puzhash,
                amount=uint64(1),
                fee_amount=uint64(fee),
                confirmed=False,
                sent=uint32(0),
                spend_bundle=spend_bundle,
                additions=add_list,
                removals=rem_list,
                wallet_id=self.id(),
                sent_to=[],
                trade_id=None,
                type=uint32(TransactionType.OUTGOING_TX.value),
                name=spend_bundle.name(),
                memos=list(compute_memos(spend_bundle).items()),
            )
        )
        return tx_list

    async def create_tandem_xch_tx(
        self,
        fee: uint64,
        announcement_to_assert: Optional[Announcement] = None,
        reuse_puzhash: Optional[bool] = None,
    ) -> TransactionRecord:
        chia_coins = await self.standard_wallet.select_coins(fee)
        if reuse_puzhash is None:
            reuse_puzhash_config = self.wallet_state_manager.config.get("reuse_public_key_for_change", None)
            if reuse_puzhash_config is None:
                reuse_puzhash = False
            else:
                reuse_puzhash = reuse_puzhash_config.get(
                    str(self.wallet_state_manager.wallet_node.logged_in_fingerprint), False
                )
        chia_tx = await self.standard_wallet.generate_signed_transaction(
            uint64(0),
            (await self.standard_wallet.get_puzzle_hash(not reuse_puzhash)),
            fee=fee,
            coins=chia_coins,
            coin_announcements_to_consume={announcement_to_assert} if announcement_to_assert is not None else None,
            reuse_puzhash=reuse_puzhash,
        )
        assert chia_tx.spend_bundle is not None
        return chia_tx

    async def select_coins(
        self,
        amount: uint64,
        exclude: Optional[List[Coin]] = None,
        min_coin_amount: Optional[uint64] = None,
        max_coin_amount: Optional[uint64] = None,
        excluded_coin_amounts: Optional[List[uint64]] = None,
    ) -> Set[Coin]:
        raise RuntimeError("NFTWallet does not support select_coins()")

    async def get_confirmed_balance(self, record_list: Optional[Set[WalletCoinRecord]] = None) -> uint128:
        """The VC wallet doesn't really have a balance."""
        return uint128(0)

    async def get_unconfirmed_balance(self, record_list: Optional[Set[WalletCoinRecord]] = None) -> uint128:
        """The VC wallet doesn't really have a balance."""
        return uint128(0)

    async def get_spendable_balance(self, unspent_records: Optional[Set[WalletCoinRecord]] = None) -> uint128:
        """The VC wallet doesn't really have a balance."""
        return uint128(0)

    async def get_pending_change_balance(self) -> uint64:
        return uint64(0)

    async def get_max_send_amount(self, records: Optional[Set[WalletCoinRecord]] = None) -> uint128:
        """This is the confirmed balance, which we set to 0 as the VC wallet doesn't have one."""
        return uint128(0)

    def puzzle_hash_for_pk(self, pubkey: G1Element) -> bytes32:
        raise RuntimeError("VCWallet does not support puzzle_hash_for_pk")

    def require_derivation_paths(self) -> bool:
        return False

    def get_name(self) -> str:
        return self.wallet_info.name


if TYPE_CHECKING:
    from chia.wallet.wallet_protocol import WalletProtocol

    _dummy: WalletProtocol = VCWallet()