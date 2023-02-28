from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional, Set

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.db_wrapper import DBWrapper2, execute_fetchone
from chia.util.ints import uint32, uint64
from chia.wallet.util.wallet_types import WalletType
from chia.wallet.wallet_merkle_coin_record import WalletMerkleCoinRecord


class WalletMerkleCoinStore:
    """
    This object handles Merkle coins in DB used by wallet.
    """

    db_wrapper: DBWrapper2

    @classmethod
    async def create(cls, wrapper: DBWrapper2):
        self = cls()

        self.db_wrapper = wrapper

        async with self.db_wrapper.writer_maybe_transaction() as conn:
            await conn.execute(
                (
                    "CREATE TABLE IF NOT EXISTS merkle_coin_record("
                    "coin_name text PRIMARY KEY,"
                    " confirmed_height bigint,"
                    " spent_height bigint,"
                    " spent int,"
                    " coin_type int,"
                    " puzzle_hash text,"
                    " metadata text,"
                    " coin_parent text,"
                    " amount blob,"
                    " wallet_type int,"
                    " wallet_id int)"
                )
            )

            await conn.execute(
                "CREATE INDEX IF NOT EXISTS merkle_coin_record_puzzlehash on merkle_coin_record(puzzle_hash)"
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS merkle_coin_record_spent on merkle_coin_record(spent)")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS merkle_coin_record_coin_type on merkle_coin_record(coin_type)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS merkle_coin_record_wallet_id on merkle_coin_record(wallet_id)"
            )

        return self

    async def count_small_unspent(self, cutoff: int) -> int:
        amount_bytes = bytes(uint64(cutoff))
        async with self.db_wrapper.reader_no_transaction() as conn:
            row = await execute_fetchone(
                conn, "SELECT COUNT(*) FROM merkle_coin_record WHERE amount < ? AND spent=0", (amount_bytes,)
            )
            return int(0 if row is None else row[0])

    # Store CoinRecord in DB and ram cache
    async def add_coin_record(self, record: WalletMerkleCoinRecord, name: Optional[bytes32] = None) -> None:
        print(record)
        if name is None:
            name = record.name()
        assert record.spent == (record.spent_block_height != 0)
        async with self.db_wrapper.writer_maybe_transaction() as conn:
            await conn.execute_insert(
                "INSERT OR REPLACE INTO merkle_coin_record VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name.hex(),
                    record.confirmed_block_height,
                    record.spent_block_height,
                    int(record.spent),
                    int(record.coin_type),
                    str(record.coin.puzzle_hash.hex()),
                    record.metadata,
                    str(record.coin.parent_coin_info.hex()),
                    bytes(uint64(record.coin.amount)),
                    record.wallet_type,
                    record.wallet_id,
                ),
            )

    # Update coin_record to be spent in DB
    async def set_spent(self, coin_name: bytes32, height: uint32) -> None:

        async with self.db_wrapper.writer_maybe_transaction() as conn:
            await conn.execute_insert(
                "UPDATE merkle_coin_record SET spent_height=?,spent=? WHERE coin_name=?",
                (
                    height,
                    1,
                    coin_name.hex(),
                ),
            )

    def coin_record_from_row(self, row: sqlite3.Row) -> WalletMerkleCoinRecord:
        coin = Coin(bytes32.fromhex(row[7]), bytes32.fromhex(row[5]), uint64.from_bytes(row[8]))
        return WalletMerkleCoinRecord(
            coin,
            uint32(row[1]),
            uint32(row[2]),
            bool(row[3]),
            row[4],
            row[6],
            WalletType(row[9]),
            row[10],
        )

    async def get_coin_record(self, coin_name: bytes32) -> Optional[WalletMerkleCoinRecord]:
        """Returns CoinRecord with specified coin id."""
        async with self.db_wrapper.reader_no_transaction() as conn:
            rows = list(
                await conn.execute_fetchall("SELECT * from merkle_coin_record WHERE coin_name=?", (coin_name.hex(),))
            )

        if len(rows) == 0:
            return None
        return self.coin_record_from_row(rows[0])

    async def get_coin_records(
        self,
        coin_names: List[bytes32],
        include_spent_coins: bool = True,
        start_height: uint32 = uint32(0),
        end_height: uint32 = uint32((2**32) - 1),
    ) -> List[Optional[WalletMerkleCoinRecord]]:
        """Returns CoinRecord with specified coin id."""
        async with self.db_wrapper.reader_no_transaction() as conn:
            rows = list(
                await conn.execute_fetchall(
                    f"SELECT * from merkle_coin_record WHERE coin_name in ({','.join('?'*len(coin_names))}) "
                    f"AND confirmed_height>=? AND confirmed_height<? "
                    f"{'' if include_spent_coins else 'AND spent=0'}",
                    tuple([c.hex() for c in coin_names]) + (start_height, end_height),
                )
            )

        ret: Dict[bytes32, WalletMerkleCoinRecord] = {}
        for row in rows:
            record = self.coin_record_from_row(row)
            coin_name = bytes32.fromhex(row[0])
            ret[coin_name] = record

        return [ret.get(name) for name in coin_names]

    async def get_coin_records_between(
        self,
        wallet_id: int,
        start,
        end,
        reverse=False,
    ) -> List[WalletMerkleCoinRecord]:
        """Return a list of merkle coins between start and end index. List is in reverse chronological order.
        start = 0 is most recent transaction
        """
        limit = end - start

        if reverse:
            query_str = "ORDER BY confirmed_height DESC "
        else:
            query_str = "ORDER BY confirmed_height ASC "

        async with self.db_wrapper.reader_no_transaction() as conn:
            rows = await conn.execute_fetchall(
                f"SELECT * FROM merkle_coin_record WHERE wallet_id=?" f" {query_str}, rowid" f" LIMIT {start}, {limit}",
                (wallet_id,),
            )

        return [self.coin_record_from_row(row) for row in rows]

    async def get_unspent_coins_for_wallet(self, wallet_id: int) -> Set[WalletMerkleCoinRecord]:
        """Returns set of CoinRecords that have not been spent yet for a wallet."""
        async with self.db_wrapper.reader_no_transaction() as conn:
            rows = await conn.execute_fetchall(
                "SELECT * FROM merkle_coin_record WHERE wallet_id=? AND spent_height=0", (wallet_id,)
            )
        return set(self.coin_record_from_row(row) for row in rows)

    async def get_all_unspent_coins(self) -> Set[WalletMerkleCoinRecord]:
        """Returns set of CoinRecords that have not been spent yet for a wallet."""
        async with self.db_wrapper.reader_no_transaction() as conn:
            rows = await conn.execute_fetchall("SELECT * FROM merkle_coin_record WHERE spent_height=0")
        return set(self.coin_record_from_row(row) for row in rows)

    async def rollback_to_block(self, height: int) -> None:
        """
        Rolls back the blockchain to block_index. All coins confirmed after this point are removed.
        All coins spent after this point are set to unspent. Can be -1 (rollback all)
        """
        print(f"Reorg {height}")
        async with self.db_wrapper.writer_maybe_transaction() as conn:
            await (await conn.execute("DELETE FROM merkle_coin_record WHERE confirmed_height>?", (height,))).close()
            await (
                await conn.execute(
                    "UPDATE merkle_coin_record SET spent_height = 0, spent = 0 WHERE spent_height>?",
                    (height,),
                )
            ).close()
