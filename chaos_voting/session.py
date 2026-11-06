"""Explicitly driven session orchestration, independent of transport and scheduling.

All calls must be serialized by the caller. The platform must notify the session
of every loss of message collection, even if it reconnects between ticks.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from math import isfinite
from random import Random

from chaos_voting.clock import Clock
from chaos_voting.game_files import GameFileError, GameProvider
from chaos_voting.platform import PlatformError, VotingPlatform
from chaos_voting.voting import RoundSnapshot, RoundState, VotingRound

logger = logging.getLogger(__name__)
_MAX_ROUND_MESSAGE_IDS = 10_000


class SessionState(Enum):
    STOPPED = auto()
    WAITING_CONNECTION = auto()
    WAITING_COMMAND = auto()
    WAITING_CATALOG = auto()
    VOTING = auto()
    ERROR = auto()


@dataclass(frozen=True)
class SessionSnapshot:
    state: SessionState
    message: str
    round: RoundSnapshot | None


class Session:
    """One session; the application drives ticks and forwards platform events.

    A platform announcement must raise PlatformError on failure. An ambiguous send failure
    stops the session rather than retrying a potentially delivered message.
    An injected clock supplies monotonic() and timezone-aware utc_now() methods.
    """

    def __init__(
        self, game: GameProvider, platform: VotingPlatform, duration: float, *,
        clock: Clock | None = None, random: Random | None = None,
    ) -> None:
        if not isfinite(duration) or duration <= 0:
            raise ValueError("Voting duration must be a positive finite number")
        self._game = game
        self._platform = platform
        self._duration = duration
        self._clock = clock if clock is not None else Clock()
        self._random = random if random is not None else Random()
        self._state = SessionState.STOPPED
        self._message = "Session stopped"
        self._round: VotingRound | None = None
        self._connected = False
        self._round_opened_at: datetime | None = None
        self._current_message_ids: set[str] = set()
        self._previous_message_ids: set[str] = set()

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(self._state, self._message,
                               self._round.snapshot() if self._round else None)

    def start(self) -> None:
        if self._state not in (SessionState.STOPPED, SessionState.ERROR):
            return
        self._round = None
        self._state = SessionState.WAITING_CONNECTION
        self._message = "Waiting for the voting platform"
        logger.info("Session started")

    def stop(self) -> None:
        if self._round is not None and self._round.snapshot().state in (RoundState.PREPARED, RoundState.OPEN):
            self._round.cancel("Session stopped")
        self._state = SessionState.STOPPED
        self._message = "Session stopped; any submitted command may still be consumed later"
        logger.info("Session stopped; any submitted command may still be consumed later")

    def connection_changed(self, connected: bool) -> None:
        if connected != self._connected:
            logger.info("Voting platform %s", "connected" if connected else "disconnected")
        self._connected = connected
        if not connected and self._state not in (SessionState.STOPPED, SessionState.ERROR):
            if self._round and self._round.snapshot().state in (RoundState.PREPARED, RoundState.OPEN):
                self._round.cancel("Voting platform disconnected; votes may be incomplete")
                logger.warning("Voting round cancelled because the platform disconnected")
            self._state = SessionState.WAITING_CONNECTION
            self._message = "Voting platform disconnected; waiting for reconnection"

    def vote(self, user_id: str, message: str) -> bool:
        return bool(self._state is SessionState.VOTING and self._round
                    and self._round.vote(user_id, message))

    def vote_event(self, user_id: str, message: str, message_id: str,
                   timestamp: datetime) -> bool:
        """Accept an identified notification only once and not before round opening.

        Timestamp is the provider notification time, not the chat publication time.
        """
        if not message_id or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("A message ID and timezone-aware timestamp are required")
        timestamp = timestamp.astimezone(UTC)
        if (self._state is not SessionState.VOTING or self._round_opened_at is None
                or timestamp < self._round_opened_at
                or message_id in self._current_message_ids
                or message_id in self._previous_message_ids):
            return False
        assert self._round is not None
        deadline = self._round.snapshot().deadline
        assert deadline is not None
        if self._clock.monotonic() >= deadline:
            return False
        if len(self._current_message_ids) >= _MAX_ROUND_MESSAGE_IDS:
            self._round.cancel("Message deduplication capacity reached")
            self._fail("Message deduplication capacity reached; voting cancelled")
            return False
        self._current_message_ids.add(message_id)
        return self.vote(user_id, message)

    def tick(self) -> None:
        if not self._connected and self._state not in (SessionState.STOPPED, SessionState.ERROR):
            self._state = SessionState.WAITING_CONNECTION
            self._message = "Waiting for the voting platform"
            return
        match self._state:
            case SessionState.STOPPED | SessionState.ERROR:
                return
            case SessionState.VOTING:
                assert self._round is not None
                deadline = self._round.snapshot().deadline
                assert deadline is not None
                if self._clock.monotonic() >= deadline:
                    self._close_round()
            case (SessionState.WAITING_CONNECTION | SessionState.WAITING_COMMAND
                  | SessionState.WAITING_CATALOG):
                self._advance_waiting()

    def _advance_waiting(self) -> None:
        try:
            if self._game.command_pending():
                if self._state is not SessionState.WAITING_COMMAND:
                    logger.info("Waiting for the game to consume the pending command")
                self._state = SessionState.WAITING_COMMAND
                self._message = "Waiting for the game to consume the command"
                return
            catalog = self._game.read_effects()
        except GameFileError as exc:
            if self._state is not SessionState.WAITING_CATALOG:
                logger.warning("Game files unavailable; waiting before retrying")
            self._state = SessionState.WAITING_CATALOG
            self._message = str(exc)
            return
        if len(set(catalog)) < 2:
            if (self._state is not SessionState.WAITING_CATALOG
                    or self._message != "At least two distinct effects are required"):
                logger.info("Waiting for at least two distinct effects")
            self._state = SessionState.WAITING_CATALOG
            self._message = "At least two distinct effects are required"
            return
        self._start_round(catalog)

    def _start_round(self, catalog: Sequence[str]) -> None:
        self._round = VotingRound(catalog, self._duration, clock=self._clock.monotonic,
                                  random=self._random)
        try:
            self._platform.announce_options(self._round.snapshot().options)
        except PlatformError:
            self._round.cancel("Could not announce options")
            self._fail("Could not announce options; session stopped")
            return
        opened_at = self._clock.utc_now()
        if opened_at.tzinfo is None or opened_at.utcoffset() is None:
            self._round.cancel("Invalid voting clock")
            self._fail("Cannot open voting: clock must include a timezone")
            return
        self._round.open()
        self._round_opened_at = opened_at.astimezone(UTC)
        self._previous_message_ids, self._current_message_ids = self._current_message_ids, set()
        self._state = SessionState.VOTING
        self._message = "Voting is open"
        logger.info("Voting options announced; session voting is open")

    def _close_round(self) -> None:
        assert self._round is not None
        winner = self._round.close()
        assert winner is not None
        try:
            self._platform.announce_result(winner)
        except PlatformError:
            self._fail("Could not announce the result; session stopped")
            return
        logger.info("Voting result announced")
        try:
            self._game.submit(winner)
        except GameFileError as exc:
            self._fail(f"Could not submit the result: {exc}")
            return
        self._state = SessionState.WAITING_COMMAND
        self._message = "Waiting for the game to consume the command"
        logger.info("Command submitted; waiting for the game to consume it")

    def _fail(self, message: str) -> None:
        self._state = SessionState.ERROR
        self._message = message
        logger.error("Session entered an error state")
