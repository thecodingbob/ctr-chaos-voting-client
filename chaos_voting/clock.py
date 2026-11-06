"""Time sources used by session orchestration."""

from datetime import UTC, datetime
from time import monotonic


class Clock:
    """Provide elapsed time and timezone-aware UTC wall time."""

    def monotonic(self) -> float:
        return monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(UTC)
