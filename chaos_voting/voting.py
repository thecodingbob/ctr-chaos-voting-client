"""Voting round rules, independent of the game and chat platform."""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from math import isfinite
from random import Random
from time import monotonic


class RoundState(Enum):
    OPEN = auto()
    CLOSED = auto()
    CANCELLED = auto()


@dataclass(frozen=True)
class RoundSnapshot:
    state: RoundState
    options: tuple[str, ...]
    counts: tuple[int, ...]
    deadline: float
    winner: str | None
    cancellation_reason: str | None


class VotingRound:
    """A single round; callers are responsible for scheduling and synchronization."""

    def __init__(
        self,
        catalog: Sequence[str],
        duration: float,
        *,
        clock: Callable[[], float] = monotonic,
        random: Random | None = None,
    ) -> None:
        if not isfinite(duration) or duration <= 0:
            raise ValueError("Voting duration must be a positive finite number")
        effects = tuple(dict.fromkeys(catalog))
        if len(effects) < 2:
            raise ValueError("At least two distinct effects are required")
        self._random = random if random is not None else Random()
        self._clock = clock
        self._options = tuple(self._random.sample(effects, min(3, len(effects))))
        self._deadline = clock() + duration
        self._votes: dict[str, int] = {}
        self._state = RoundState.OPEN
        self._winner: str | None = None
        self._cancellation_reason: str | None = None

    def vote(self, user_id: str, message: str) -> bool:
        """Record the latest valid vote; reject messages outside the voting window."""
        if self._state is not RoundState.OPEN or self._clock() >= self._deadline:
            return False
        if not user_id or message not in ("1", "2", "3"):
            return False
        index = int(message) - 1
        if index >= len(self._options):
            return False
        self._votes[user_id] = index
        return True

    def close(self) -> str | None:
        """Close an expired round and return its winner, consistently on retries."""
        if self._state is RoundState.CANCELLED:
            raise RuntimeError("A cancelled round cannot be closed")
        if self._state is RoundState.CLOSED:
            assert self._winner is not None
            return self._winner
        if self._clock() < self._deadline:
            raise RuntimeError("The voting window is still open")
        counts = Counter(self._votes.values())
        highest = max(counts.values(), default=0)
        candidates = [
            option for index, option in enumerate(self._options) if counts[index] == highest
        ]
        self._winner = self._random.choice(candidates)
        self._state = RoundState.CLOSED
        return self._winner

    def cancel(self, reason: str) -> None:
        """Cancel an open round, including one whose deadline has elapsed."""
        if self._state is not RoundState.OPEN:
            raise RuntimeError("Only an open round can be cancelled")
        if not reason:
            raise ValueError("A cancellation reason is required")
        self._cancellation_reason = reason
        self._state = RoundState.CANCELLED

    def snapshot(self) -> RoundSnapshot:
        counts = Counter(self._votes.values())
        return RoundSnapshot(
            state=self._state,
            options=self._options,
            counts=tuple(counts[index] for index in range(len(self._options))),
            deadline=self._deadline,
            winner=self._winner,
            cancellation_reason=self._cancellation_reason,
        )
