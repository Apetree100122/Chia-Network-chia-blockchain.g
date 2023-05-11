from __future__ import annotations

from typing import Dict, Tuple

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import INFINITE_COST, Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.types.condition_opcodes import ConditionOpcode
from chia.util.ints import uint64


def compute_coin_hints(cs: CoinSpend) -> Tuple[Dict[bytes32, bytes32], Dict[bytes32, Coin]]:
    _, result_program = cs.puzzle_reveal.run_with_cost(INFINITE_COST, cs.solution)

    hint_dict: Dict[bytes32, bytes32] = {}  # {coin_id: hint}
    coin_dict: Dict[bytes32, Coin] = {}  # {coin_id: Coin}
    for condition in result_program.as_iter():
        if (
            condition.at("f").atom == ConditionOpcode.CREATE_COIN  # It's a create coin
            and condition.at("rrr") != Program.to(None)  # There's more than two arguments
            and condition.at("rrrf").atom is None  # The 3rd argument is a cons
        ):
            potential_hint: bytes = condition.at("rrrff").atom
            if len(potential_hint) == 32:
                coin: Coin = Coin(
                    cs.coin.name(), bytes32(condition.at("rf").atom), uint64(condition.at("rrf").as_int())
                )
                coin_id: bytes32 = coin.name()
                hint_dict[coin_id] = bytes32(potential_hint)
                coin_dict[coin_id] = coin

    return hint_dict, coin_dict
