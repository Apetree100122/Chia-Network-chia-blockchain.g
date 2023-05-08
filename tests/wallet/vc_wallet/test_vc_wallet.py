from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal, Optional

import pytest
from blspy import G2Element

from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.simulator.full_node_simulator import FullNodeSimulator
from chia.simulator.time_out_assert import time_out_assert_not_none
from chia.types.blockchain_format.coin import coin_as_list
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.types.peer_info import PeerInfo
from chia.types.spend_bundle import SpendBundle
from chia.util.bech32m import encode_puzzle_hash
from chia.util.ints import uint16, uint64
from chia.wallet.cat_wallet.cat_utils import construct_cat_puzzle
from chia.wallet.did_wallet.did_wallet import DIDWallet
from chia.wallet.puzzles.cat_loader import CAT_MOD
from chia.wallet.util.wallet_types import WalletType
from chia.wallet.vc_wallet.cr_cat_drivers import ProofsChecker, construct_cr_layer
from chia.wallet.vc_wallet.cr_cat_wallet import CRCATWallet
from chia.wallet.vc_wallet.vc_store import VCProofs, VCRecord


@pytest.mark.parametrize(
    "trusted",
    [True, False],
)
@pytest.mark.asyncio
async def test_vc_lifecycle(self_hostname: str, two_wallet_nodes_services: Any, trusted: Any) -> None:
    num_blocks = 1
    full_nodes, wallets, bt = two_wallet_nodes_services
    full_node_api: FullNodeSimulator = full_nodes[0]._api
    full_node_server = full_node_api.full_node.server
    wallet_service_0 = wallets[0]
    wallet_service_1 = wallets[1]
    wallet_node_0 = wallet_service_0._node
    wallet_node_1 = wallet_service_1._node
    wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_0.wallet_state_manager.main_wallet

    client_0 = await WalletRpcClient.create(
        bt.config["self_hostname"],
        wallet_service_0.rpc_server.listen_port,
        wallet_service_0.root_path,
        wallet_service_0.config,
    )
    wallet_node_0.config["automatically_add_unknown_cats"] = True
    wallet_node_1.config["automatically_add_unknown_cats"] = True

    if trusted:
        wallet_node_0.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_1.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}

    await wallet_node_0.server.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await wallet_node_1.server.start_client(PeerInfo(self_hostname, uint16(full_node_server._port)), None)
    await full_node_api.farm_blocks_to_wallet(count=num_blocks, wallet=wallet_0)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=20)
    did_wallet: DIDWallet = await DIDWallet.create_new_did_wallet(
        wallet_node_0.wallet_state_manager, wallet_0, uint64(1)
    )
    spend_bundle_list = await wallet_node_0.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(did_wallet.id())

    spend_bundle = spend_bundle_list[0].spend_bundle
    assert spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle.name())

    await full_node_api.farm_blocks_to_wallet(count=num_blocks, wallet=wallet_0)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=20)
    did_id = bytes32.from_hexstr(did_wallet.get_my_DID())
    vc_record, txs = await client_0.vc_mint(did_id, target_address=await wallet_0.get_new_puzzlehash())
    spend_bundle = next(tx.spend_bundle for tx in txs if tx.spend_bundle is not None)
    await time_out_assert_not_none(30, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle.name())
    await full_node_api.farm_blocks_to_wallet(count=num_blocks, wallet=wallet_0)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=20)
    vc_wallet = await wallet_node_0.wallet_state_manager.get_all_wallet_info_entries(wallet_type=WalletType.VC)
    assert len(vc_wallet) == 1
    new_vc_record: Optional[VCRecord] = await client_0.vc_get(vc_record.vc.launcher_id)
    assert new_vc_record is not None

    # Spend VC
    proofs: VCProofs = VCProofs({"foo": "1", "bar": "1"})
    proof_root: bytes32 = proofs.root()
    txs = await client_0.vc_spend(
        vc_record.vc.launcher_id,
        new_proof_hash=proof_root,
        fee=uint64(100),
    )
    spend_bundle = next(tx.spend_bundle for tx in txs if tx.spend_bundle is not None)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle.name())
    await full_node_api.farm_blocks_to_wallet(count=num_blocks, wallet=wallet_0)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=20)
    vc_record_updated: Optional[VCRecord] = await client_0.vc_get(vc_record.vc.launcher_id)
    assert vc_record_updated is not None
    assert vc_record_updated.vc.proof_hash == proof_root

    # Do a mundane spend
    txs = await client_0.vc_spend(vc_record.vc.launcher_id)
    spend_bundle = next(tx.spend_bundle for tx in txs if tx.spend_bundle is not None)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle.name())
    await full_node_api.farm_blocks_to_wallet(count=num_blocks, wallet=wallet_0)
    vc_record_updated = await client_0.vc_get(vc_record.vc.launcher_id)
    assert vc_record_updated is not None

    # Add proofs to DB
    await client_0.vc_add_proofs(proofs.key_value_pairs)
    assert await client_0.vc_get_proofs_for_root(proof_root) == proofs.key_value_pairs
    vc_records, fetched_proofs = await client_0.vc_get_list()
    assert len(vc_records) == 1
    assert fetched_proofs[proof_root.hex()] == proofs.key_value_pairs

    # Mint CR-CAT
    our_puzzle: Program = await wallet_0.get_new_puzzle()
    proofs_checker: ProofsChecker = ProofsChecker(["foo", "bar"])
    cat_puzzle: Program = construct_cat_puzzle(
        CAT_MOD,
        Program.to(None).get_tree_hash(),
        Program.to(1),
    )
    addr = encode_puzzle_hash(cat_puzzle.get_tree_hash(), "txch")
    CAT_AMOUNT_0 = uint64(100)

    tx = await client_0.send_transaction(1, CAT_AMOUNT_0, addr)
    spend_bundle = tx.spend_bundle
    assert spend_bundle is not None

    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle.name())
    await full_node_api.farm_blocks_to_wallet(count=num_blocks, wallet=wallet_0)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=20)

    # Do the eve spend back to our wallet and add the CR layer
    cat_coin = next(c for c in spend_bundle.additions() if c.amount == CAT_AMOUNT_0)
    eve_spend = SpendBundle(
        [
            CoinSpend(
                cat_coin,
                cat_puzzle,
                Program.to(
                    [
                        Program.to(
                            [
                                [
                                    51,
                                    construct_cr_layer(
                                        [did_id],
                                        proofs_checker.as_program(),
                                        our_puzzle,
                                    ).get_tree_hash(),
                                    CAT_AMOUNT_0,
                                    [our_puzzle.get_tree_hash()],
                                ],
                                [51, None, -113, None, None],
                                [1, our_puzzle.get_tree_hash(), [did_id], proofs_checker.as_program()],
                            ]
                        ),
                        None,
                        cat_coin.name(),
                        coin_as_list(cat_coin),
                        [cat_coin.parent_coin_info, Program.to(1).get_tree_hash(), cat_coin.amount],
                        0,
                        0,
                    ]
                ),
            )
        ],
        G2Element(),
    )
    await client_0.push_tx(eve_spend)  # type: ignore [no-untyped-call]
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, eve_spend.name())
    await full_node_api.farm_blocks_to_wallet(count=num_blocks, wallet=wallet_0)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=20)

    # Send CR-CAT to another wallet
    async def check_length(length: int, func: Callable[..., Awaitable[Any]], *args: Any) -> Optional[Literal[True]]:
        if len(await func(*args)) == length:
            return True
        return None

    await time_out_assert_not_none(
        15, check_length, 1, wallet_node_0.wallet_state_manager.get_all_wallet_info_entries, WalletType.CRCAT
    )
    cr_cat_wallet_id: uint16 = (
        await wallet_node_0.wallet_state_manager.get_all_wallet_info_entries(wallet_type=WalletType.CRCAT)
    )[0].id
    cr_cat_wallet: CRCATWallet = wallet_node_0.wallet_state_manager.wallets[cr_cat_wallet_id]
    assert await wallet_node_0.wallet_state_manager.get_wallet_for_asset_id(cr_cat_wallet.get_asset_id()) is not None
    tx = await client_0.cat_spend(
        cr_cat_wallet.id(),
        uint64(100),
        encode_puzzle_hash(await wallet_1.get_new_puzzlehash(), "txch"),
        uint64(2000000000),
        memos=["hey"],
    )
    await wallet_node_0.wallet_state_manager.add_pending_transaction(tx)
    assert tx.spend_bundle is not None
    spend_bundle = tx.spend_bundle
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle.name())
    await full_node_api.farm_blocks_to_wallet(count=num_blocks, wallet=wallet_0)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=20)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_1, timeout=20)

    # Check the other wallet recieved it
    assert len(await wallet_node_0.wallet_state_manager.get_all_wallet_info_entries(wallet_type=WalletType.CRCAT)) == 1

    # Revoke VC
    vc_record_updated = await client_0.vc_get(vc_record_updated.vc.launcher_id)
    assert vc_record_updated is not None
    txs = await client_0.vc_revoke(vc_record_updated.vc.coin.parent_coin_info, uint64(1))
    spend_bundle = next(tx.spend_bundle for tx in txs if tx.spend_bundle is not None)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle.name())
    await full_node_api.farm_blocks_to_wallet(count=num_blocks, wallet=wallet_0)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=20)
    vc_record_revoked: Optional[VCRecord] = await client_0.vc_get(vc_record.vc.launcher_id)
    assert vc_record_revoked is None
