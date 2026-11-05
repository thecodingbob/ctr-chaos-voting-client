"""Contract for voting platform announcements."""

from typing import Protocol


class PlatformError(Exception):
    """An announcement failed or its delivery could not be determined."""


class VotingPlatform(Protocol):
    """Announce rounds and results, raising PlatformError on failure."""

    def announce_options(self, options: tuple[str, ...]) -> None: ...
    def announce_result(self, winner: str) -> None: ...
