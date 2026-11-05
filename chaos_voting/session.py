"""Explicitly driven session orchestration, independent of transport and scheduling.

All calls must be serialized by the caller. The platform must notify the session
of every loss of message collection, even if it reconnects between ticks.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from math import isfinite
from random import Random
from time import monotonic

from chaos_voting.game_files import GameFileError, GameProvider
from chaos_voting.platform import PlatformError, VotingPlatform
from chaos_voting.voting import RoundSnapshot, RoundState, VotingRound

logger = logging.getLogger(__name__)


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
    """

    def __init__(
        self, game: GameProvider, platform: VotingPlatform, duration: float, *,
        clock: Callable[[], float] = monotonic, random: Random | None = None,
    ) -> None:
        if not isfinite(duration) or duration <= 0:
            raise ValueError("Voting duration must be a positive finite number")
        self._game = game
        self._platform = platform
        self._duration = duration
        self._clock = clock
        self._random = random if random is not None else Random()
        self._state = SessionState.STOPPED
        self._message = "Session stopped"
        self._round: VotingRound | None = None
        self._connected = False

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
        if self._round and self._round.snapshot().state in (RoundState.PREPARED, RoundState.OPEN):
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

    def tick(self) -> None:
        if self._state in (SessionState.STOPPED, SessionState.ERROR):
            return
        if not self._connected:
            self._state = SessionState.WAITING_CONNECTION
            self._message = "Waiting for the voting platform"
            return
        if self._state is SessionState.VOTING:
            assert self._round is not None
            deadline = self._round.snapshot().deadline
            assert deadline is not None
            if self._clock() < deadline:
                return
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
            return
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
        self._round = VotingRound(catalog, self._duration, clock=self._clock, random=self._random)
        try:
            self._platform.announce_options(self._round.snapshot().options)
        except PlatformError:
            self._round.cancel("Could not announce options")
            self._fail("Could not announce options; session stopped")
            return
        self._round.open()
        self._state = SessionState.VOTING
        self._message = "Voting is open"
        logger.info("Voting options announced; session voting is open")

    def _fail(self, message: str) -> None:
        self._state = SessionState.ERROR
        self._message = message
        logger.error("Session stopped after an announcement or command submission failure")
