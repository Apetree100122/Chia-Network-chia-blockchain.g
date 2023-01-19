# General puzzle wrapper
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from typing_extensions import Protocol

from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.wallet.puzzles.clawback.clawback_puzzle_decorator import ClawbackPuzzleDecorator


class PuzzleDecoratorType(Enum):
    # Puzzle Decorator Types
    CLAWBACK = 1


class PuzzleDecoratorProtocol(Protocol):
    # Protocol for puzzle decorators
    @staticmethod
    def create(config: Dict[str, Any]) -> PuzzleDecoratorProtocol:
        ...

    def decorate(self, inner_puzzle: Program) -> Program:
        ...

    def decorate_target_puzhash(self, inner_puzzle: Program, target_puzhash: bytes32) -> Tuple[Program, bytes32]:
        ...

    def decorate_memos(
        self, inner_puzzle: Program, target_puzhash: bytes32, memos: List[bytes]
    ) -> Tuple[Program, List[bytes]]:
        ...

    def solve(
        self, puzzle: Program, primaries: List[Dict[str, Any]], inner_solution: Program
    ) -> Tuple[Program, Program]:
        ...


class PuzzleDecoratorManager:
    # Clawback
    decorator_list: List[PuzzleDecoratorProtocol]

    @staticmethod
    def create(config: Dict[str, Any], fingerprint: Optional[int]) -> PuzzleDecoratorManager:
        """
        Create a new puzzle decorator manager
        :param config: Config
        :param fingerprint: Fingerprint
        :return:
        """
        self = PuzzleDecoratorManager()
        self.decorator_list = []
        assert fingerprint is not None
        decorator_config: Dict[int, List[str]] = config.get("puzzle_decorators", {})
        print(f"Initial puzzle decorator: {decorator_config}")
        if fingerprint in decorator_config:
            for decorator in decorator_config.get(fingerprint, []):
                if decorator == PuzzleDecoratorType.CLAWBACK.name:
                    self.decorator_list.append(ClawbackPuzzleDecorator.create(config))
                else:
                    raise ValueError(f"Unknown puzzle decorator type: {decorator}")
        return self

    def decorate(self, inner_puzzle: Program) -> Program:
        """
        Decorator a puzzle
        :param inner_puzzle: Inner puzzle
        :return: Decorated inner puzzle
        """
        for decorator in self.decorator_list:
            inner_puzzle = decorator.decorate(inner_puzzle)
        return inner_puzzle

    def decorate_target_puzhash(self, inner_puzzle: Program, target_puzhash: bytes32) -> bytes32:
        """
        Decorate a target puzzle hash
        :param target_puzhash: Target puzzle hash
        :param inner_puzzle: Inner puzzle
        :return: Decorated target puzzle hash
        """
        for decorator in self.decorator_list:
            inner_puzzle, target_puzhash = decorator.decorate_target_puzhash(inner_puzzle, target_puzhash)
        return target_puzhash

    def solve(self, inner_puzzle: Program, primaries: List[Dict[str, Any]], inner_solution: Program) -> Program:
        """
        Generate the solution of the puzzle
        :param inner_puzzle: Inner puzzle
        :param primaries: Primaries list
        :param inner_solution: Solution of the inner puzzle
        :return: Decorated inner puzzle solution
        """
        for decorator in self.decorator_list:
            inner_puzzle, inner_solution = decorator.solve(inner_puzzle, primaries, inner_solution)
        return inner_solution

    def decorate_memos(self, inner_puzzle: Program, target_puzhash: bytes32, memos: List[bytes]) -> List[bytes]:
        """
        Decorate a memo list
        :param inner_puzzle: Inner puzzle
        :param target_puzhash: Target puzzle hash
        :param memos: memo list
        :return: Decorated memo
        """
        for decorator in self.decorator_list:
            inner_puzzle, memos = decorator.decorate_memos(inner_puzzle, target_puzhash, memos)
        return memos