import asyncio
import contextlib
import itertools
import time
from typing import Collection, Iterator, List, Optional, Set, Union

import anyio

from chia.consensus.block_record import BlockRecord
from chia.consensus.block_rewards import calculate_base_farmer_reward, calculate_pool_reward
from chia.consensus.multiprocess_validation import PreValidationResult
from chia.full_node.full_node_api import FullNodeAPI
from chia.protocols.full_node_protocol import RespondBlock
from chia.simulator.simulator_protocol import FarmNewBlockProtocol, ReorgProtocol
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.full_block import FullBlock
from chia.util.api_decorators import api_request
from chia.util.ints import uint8, uint32, uint64
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.util.wallet_types import AmountWithPuzzlehash
from chia.wallet.wallet import Wallet


class _Default:
    pass


default = _Default()


@contextlib.contextmanager
def fail_after(delay: Optional[float], shield: bool = False) -> Iterator[None]:
    if delay is not None:
        delay *= 10
        clock = time.monotonic
        start = clock()
    try:
        with anyio.fail_after(delay=delay, shield=shield):
            yield
    finally:
        if delay is not None:
            end = clock()
            duration = end - start
            ratio = duration / delay
            print(f" ==== {duration=} / {delay=} = {ratio:.2}")


def backoff_times(
    initial: float = 0.001,
    final: float = 0.100,
    time_to_final: float = 0.5,
    clock=time.monotonic,
) -> Iterator[float]:
    # initially implemented as a simple linear backoff

    start = clock()
    delta = 0

    result_range = final - initial

    while True:
        yield min(final, initial + ((delta / time_to_final) * result_range))
        delta = clock() - start


async def wait_for_coins_in_wallet(coins: Set[Coin], wallet: Wallet, timeout: Optional[float] = 5):
    """Wait until all of the specified coins are simultaneously reported as spendable
    by the wallet.

    Arguments:
        coins: The coins expected to be received.
        wallet: The wallet expected to receive the coins.
    """
    with fail_after(delay=timeout):
        for backoff in backoff_times():
            spendable_wallet_coin_records = await wallet.wallet_state_manager.get_spendable_coins_for_wallet(
                wallet_id=wallet.id()
            )
            spendable_wallet_coins = {record.coin for record in spendable_wallet_coin_records}

            if coins.issubset(spendable_wallet_coins):
                return

            await asyncio.sleep(backoff)


class FullNodeSimulator(FullNodeAPI):
    def __init__(self, full_node, block_tools) -> None:
        super().__init__(full_node)
        self.bt = block_tools
        self.full_node = full_node
        self.config = full_node.config
        self.time_per_block: Optional[float] = None
        if "simulation" in self.config and self.config["simulation"] is True:
            self.use_current_time = True
        else:
            self.use_current_time = False

    async def get_all_full_blocks(self) -> List[FullBlock]:
        peak: Optional[BlockRecord] = self.full_node.blockchain.get_peak()
        if peak is None:
            return []
        blocks = []
        peak_block = await self.full_node.blockchain.get_full_block(peak.header_hash)
        if peak_block is None:
            return []
        blocks.append(peak_block)
        current = peak_block
        while True:
            prev = await self.full_node.blockchain.get_full_block(current.prev_header_hash)
            if prev is not None:
                current = prev
                blocks.append(prev)
            else:
                break

        blocks.reverse()
        return blocks

    @api_request
    async def farm_new_transaction_block(self, request: FarmNewBlockProtocol):
        async with self.full_node._blockchain_lock_high_priority:
            self.log.info("Farming new block!")
            current_blocks = await self.get_all_full_blocks()
            if len(current_blocks) == 0:
                genesis = self.bt.get_consecutive_blocks(uint8(1))[0]
                pre_validation_results: List[
                    PreValidationResult
                ] = await self.full_node.blockchain.pre_validate_blocks_multiprocessing(
                    [genesis], {}, validate_signatures=True
                )
                assert pre_validation_results is not None
                await self.full_node.blockchain.receive_block(genesis, pre_validation_results[0])

            peak = self.full_node.blockchain.get_peak()
            assert peak is not None
            curr: BlockRecord = peak
            while not curr.is_transaction_block:
                curr = self.full_node.blockchain.block_record(curr.prev_hash)
            mempool_bundle = await self.full_node.mempool_manager.create_bundle_from_mempool(curr.header_hash)
            if mempool_bundle is None:
                spend_bundle = None
            else:
                spend_bundle = mempool_bundle[0]

            current_blocks = await self.get_all_full_blocks()
            target = request.puzzle_hash
            more = self.bt.get_consecutive_blocks(
                1,
                time_per_block=self.time_per_block,
                transaction_data=spend_bundle,
                farmer_reward_puzzle_hash=target,
                pool_reward_puzzle_hash=target,
                block_list_input=current_blocks,
                guarantee_transaction_block=True,
                current_time=self.use_current_time,
                previous_generator=self.full_node.full_node_store.previous_generator,
            )
            rr = RespondBlock(more[-1])
        await self.full_node.respond_block(rr)

    @api_request
    async def farm_new_block(self, request: FarmNewBlockProtocol):
        async with self.full_node._blockchain_lock_high_priority:
            self.log.info("Farming new block!")
            current_blocks = await self.get_all_full_blocks()
            if len(current_blocks) == 0:
                genesis = self.bt.get_consecutive_blocks(uint8(1))[0]
                pre_validation_results: List[
                    PreValidationResult
                ] = await self.full_node.blockchain.pre_validate_blocks_multiprocessing(
                    [genesis], {}, validate_signatures=True
                )
                assert pre_validation_results is not None
                await self.full_node.blockchain.receive_block(genesis, pre_validation_results[0])

            peak = self.full_node.blockchain.get_peak()
            assert peak is not None
            curr: BlockRecord = peak
            while not curr.is_transaction_block:
                curr = self.full_node.blockchain.block_record(curr.prev_hash)
            mempool_bundle = await self.full_node.mempool_manager.create_bundle_from_mempool(curr.header_hash)
            if mempool_bundle is None:
                spend_bundle = None
            else:
                spend_bundle = mempool_bundle[0]
            current_blocks = await self.get_all_full_blocks()
            target = request.puzzle_hash
            more = self.bt.get_consecutive_blocks(
                1,
                transaction_data=spend_bundle,
                farmer_reward_puzzle_hash=target,
                pool_reward_puzzle_hash=target,
                block_list_input=current_blocks,
                current_time=self.use_current_time,
            )
            rr: RespondBlock = RespondBlock(more[-1])
        await self.full_node.respond_block(rr)

    @api_request
    async def reorg_from_index_to_new_index(self, request: ReorgProtocol):
        new_index = request.new_index
        old_index = request.old_index
        coinbase_ph = request.puzzle_hash

        current_blocks = await self.get_all_full_blocks()
        block_count = new_index - old_index

        more_blocks = self.bt.get_consecutive_blocks(
            block_count,
            farmer_reward_puzzle_hash=coinbase_ph,
            pool_reward_puzzle_hash=coinbase_ph,
            block_list_input=current_blocks[: old_index + 1],
            force_overflow=True,
            guarantee_transaction_block=True,
            seed=32 * b"1",
        )

        for block in more_blocks:
            await self.full_node.respond_block(RespondBlock(block))

    async def farm_blocks_to_puzzlehash(
        self,
        count: int,
        farm_to: bytes32 = bytes32([0] * 32),
        guarantee_transaction_blocks: bool = False,
        timeout: Union[None, _Default, float] = default,
    ) -> int:
        """Process the requested number of blocks including farming to the passed puzzle
        hash. Note that the rewards for the last block will not have been processed.
        Consider `.farm_blocks()` or `.farm_rewards()` if the goal is to receive XCH at
        an address.

        Arguments:
            count: The number of blocks to process.
            farm_to: The puzzle hash to farm the block rewards to.

        Returns:
            The total number of reward mojos for the processed blocks.
        """
        if isinstance(timeout, _Default):
            timeout = count * 1
            timeout += 1

        with fail_after(delay=timeout):
            rewards = 0

            if count == 0:
                return rewards

            for _ in range(count):
                if guarantee_transaction_blocks:
                    await self.farm_new_transaction_block(FarmNewBlockProtocol(farm_to))
                else:
                    await self.farm_new_block(FarmNewBlockProtocol(farm_to))
                # hopefully there is no race between the
                height = self.full_node.blockchain.get_peak_height()

                if height is None:
                    raise RuntimeError("Peak height still None after processing at least one block")

                rewards += calculate_pool_reward(height) + calculate_base_farmer_reward(height)

            return rewards

    async def farm_blocks_to_wallet(
        self,
        count: int,
        wallet: Wallet,
        timeout: Union[None, _Default, float] = default,
    ) -> int:
        """Farm the requested number of blocks to the passed wallet. This will
        process additional blocks as needed to process the reward transactions
        and also wait for the rewards to be present in the wallet.

        Arguments:
            count: The number of blocks to farm.
            wallet: The wallet to farm the block rewards to.

        Returns:
            The total number of reward mojos farmed to the requested address.
        """
        if isinstance(timeout, _Default):
            timeout = (count + 1) * 1
            timeout += 5

        with fail_after(delay=timeout):
            if count == 0:
                return 0

            target_puzzlehash = await wallet.get_new_puzzlehash()
            rewards = 0

            block_reward_coins = set()
            expected_reward_coin_count = 2 * count

            # TODO: why two final transaction blocks and not just one?
            for to_wallet in [*([True] * count), False, False]:
                if to_wallet:
                    rewards += await self.farm_blocks_to_puzzlehash(
                        count=1, farm_to=target_puzzlehash, guarantee_transaction_blocks=False
                    )
                else:
                    await self.farm_blocks_to_puzzlehash(count=1, guarantee_transaction_blocks=True, timeout=None)

                peak_height = self.full_node.blockchain.get_peak_height()
                if peak_height is None:
                    raise RuntimeError("Peak height still None after processing at least one block")

                coin_records = await self.full_node.coin_store.get_coins_added_at_height(height=peak_height)
                for record in coin_records:
                    if record.coin.puzzle_hash == target_puzzlehash and record.coinbase:
                        block_reward_coins.add(record.coin)

                if len(block_reward_coins) >= expected_reward_coin_count:
                    break
            else:
                raise RuntimeError("Not all reward coins identified")

            if len(block_reward_coins) != expected_reward_coin_count:
                raise RuntimeError(
                    f"Expected {expected_reward_coin_count} reward coins, got: {len(block_reward_coins)}"
                )

            await wait_for_coins_in_wallet(coins=block_reward_coins, wallet=wallet, timeout=None)

            return rewards

    async def farm_rewards_to_wallet(
        self,
        amount: int,
        wallet: Wallet,
        timeout: Union[None, _Default, float] = default,
    ) -> int:
        """Farm at least the requested amount of mojos to the passed wallet. Extra
        mojos will be received based on the block rewards at the present block height.
        The rewards will be present in the wall before returning.

        Arguments:
            amount: The minimum number of mojos to farm.
            wallet: The wallet to farm the block rewards to.

        Returns:
            The total number of reward mojos farmed to the requested wallet.
        """
        rewards = 0

        if amount == 0:
            return rewards

        height_before = self.full_node.blockchain.get_peak_height()
        if height_before is None:
            height_before = uint32(0)

        for count in itertools.count(1):
            height = uint32(height_before + count)
            rewards += calculate_pool_reward(height) + calculate_base_farmer_reward(height)

            if rewards >= amount:
                break
        else:
            raise Exception("internal error")

        if isinstance(timeout, _Default):
            timeout = (count + 1) * 1

        with fail_after(delay=timeout):
            await self.farm_blocks_to_wallet(count=count, wallet=wallet, timeout=None)
            return rewards

    async def wait_transaction_records_entered_mempool(
        self,
        records: Collection[TransactionRecord],
        timeout: Union[None, float] = 5,
    ) -> None:
        """Wait until the transaction records have entered the mempool.  Transaction
        records with no spend bundle are ignored.

        Arguments:
            records: The transaction records to wait for.
        """
        with fail_after(delay=timeout):
            ids_to_check: Set[bytes32] = set()
            for record in records:
                if record.spend_bundle is None:
                    continue

                ids_to_check.add(record.spend_bundle.name())

            for backoff in backoff_times():
                found = set()
                for spend_bundle_name in ids_to_check:
                    tx = self.full_node.mempool_manager.get_spendbundle(spend_bundle_name)
                    if tx is not None:
                        found.add(spend_bundle_name)
                ids_to_check = ids_to_check.difference(found)

                if len(ids_to_check) == 0:
                    return

                await asyncio.sleep(backoff)

    async def process_transaction_records(
        self,
        records: Collection[TransactionRecord],
        timeout: Union[None, float] = 5,
    ) -> None:
        """Process the specified transaction records and wait until they have been
        included in a block.

        Arguments:
            records: The transaction records to process.
        """
        with fail_after(delay=timeout):
            coins_to_wait_for: Set[Coin] = set()
            for record in records:
                if record.spend_bundle is None:
                    continue

                coins_to_wait_for.update(record.spend_bundle.additions())

            coin_store = self.full_node.coin_store

            await self.wait_transaction_records_entered_mempool(records=records, timeout=None)

            while True:
                await self.farm_blocks_to_puzzlehash(count=1, timeout=None)

                found: Set[Coin] = set()
                for coin in coins_to_wait_for:
                    # TODO: is this the proper check?
                    if await coin_store.get_coin_record(coin.name()) is not None:
                        found.add(coin)

                coins_to_wait_for = coins_to_wait_for.difference(found)

                if len(coins_to_wait_for) == 0:
                    return

    async def create_coins_with_amounts(
        self,
        amounts: List[uint64],
        wallet: Wallet,
        per_transaction_record_group: int = 50,
        timeout: Union[None, float] = 15,
    ) -> Set[Coin]:
        """Create coins with the requested amount.  This is useful when you need a
        bunch of coins for a test and don't need to farm that many.

        Arguments:
            amounts: A list with entries of mojo amounts corresponding to each
                coin to create.
            wallet: The wallet to send the new coins to.
            per_transaction_record_group: The maximum number of coins to create in each
                transaction record.

        Returns:
            A set of the generated coins.  Note that this does not include any change
            coins that were created.
        """
        with fail_after(delay=timeout):
            invalid_amounts = [amount for amount in amounts if amount <= 0]
            if len(invalid_amounts) > 0:
                invalid_amounts_string = ", ".join(str(amount) for amount in invalid_amounts)
                raise Exception(f"Coins must have a positive value, request included: {invalid_amounts_string}")

            if len(amounts) == 0:
                return set()

            # TODO: This is a poor duplication of code in
            #       WalletRpcApi.create_signed_transaction().  Perhaps it should be moved
            #       somewhere more reusable.

            outputs: List[AmountWithPuzzlehash] = []
            for amount in amounts:
                puzzle_hash = await wallet.get_new_puzzlehash()
                outputs.append({"puzzlehash": puzzle_hash, "amount": uint64(amount), "memos": []})

            transaction_records: List[TransactionRecord] = []
            outputs_iterator = iter(outputs)
            while True:
                # The outputs iterator must be second in the zip() call otherwise we lose
                # an element when reaching the end of the range object.
                outputs_group = [output for _, output in zip(range(per_transaction_record_group), outputs_iterator)]

                if len(outputs_group) > 0:
                    async with wallet.wallet_state_manager.lock:
                        tx = await wallet.generate_signed_transaction(
                            amount=outputs_group[0]["amount"],
                            puzzle_hash=outputs_group[0]["puzzlehash"],
                            primaries=outputs_group[1:],
                        )
                    await wallet.push_transaction(tx=tx)
                    transaction_records.append(tx)
                else:
                    break

            await self.process_transaction_records(records=transaction_records, timeout=None)

            output_coins = {coin for transaction_record in transaction_records for coin in transaction_record.additions}
            puzzle_hashes = {output["puzzlehash"] for output in outputs}
            change_coins = {coin for coin in output_coins if coin.puzzle_hash not in puzzle_hashes}
            coins_to_receive = output_coins - change_coins
            await wait_for_coins_in_wallet(coins=coins_to_receive, wallet=wallet)

            return coins_to_receive
