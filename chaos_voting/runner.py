"""Blocking session loop with interruptible waits and explicit reconnection."""

import logging
from collections.abc import Callable
from math import isfinite
from threading import Event, Lock

from chaos_voting.session import Session, SessionState
from chaos_voting.twitch.eventsub import EventSubError, TwitchEventSub

logger = logging.getLogger(__name__)
_TICK_INTERVAL = 0.1
_RECONNECT_INTERVAL = 5.0
_MAX_MESSAGES_PER_TICK = 10_000


class SessionRunner:
    """Run on one thread; request shutdown from another with the stop event.

    Session and receiver calls must not be made from other threads while running.
    Network operations can delay shutdown until their own timeout. Create a new,
    unset event for each run. Notification timestamps are not publication times,
    and draining available messages cannot guarantee timely delivery by Twitch.
    """

    def __init__(
        self, session: Session, client_id: str, access_token: str, user_id: str, *,
        receiver_factory: Callable = TwitchEventSub,
        tick_interval: float = _TICK_INTERVAL,
        reconnect_interval: float = _RECONNECT_INTERVAL,
    ) -> None:
        if not all(isfinite(value) and value > 0 for value in (tick_interval, reconnect_interval)):
            raise ValueError("Intervals must be positive finite numbers")
        self._session = session
        self._receiver = receiver_factory(
            client_id, access_token, user_id,
            on_connection=session.connection_changed, on_message=session.vote_event,
        )
        self._tick_interval = tick_interval
        self._reconnect_interval = reconnect_interval
        self._running = Lock()

    def run(self, stop: Event) -> None:
        """Start explicitly and run until stop is set; do not call concurrently."""
        if not self._running.acquire(blocking=False):
            raise RuntimeError("Session runner is already running")
        try:
            if stop.is_set():
                return
            connected = False
            try:
                self._session.start()
                while not stop.is_set():
                    if not connected:
                        try:
                            self._connect()
                        except EventSubError:
                            logger.warning("EventSub connection failed; waiting to retry")
                            stop.wait(self._reconnect_interval)
                            continue
                        connected = True
                    if stop.is_set():
                        break
                    try:
                        self._tick()
                    except EventSubError:
                        connected = False
                        if self._session.snapshot().state is SessionState.ERROR:
                            logger.error("Session failed; stopping runner")
                            break
                        logger.warning("EventSub interrupted; waiting to reconnect")
                        stop.wait(self._reconnect_interval)
                        continue
                    if self._session.snapshot().state is SessionState.ERROR:
                        logger.error("Session failed; stopping runner")
                        break
                    stop.wait(self._tick_interval)
            finally:
                try:
                    if self._session.snapshot().state is not SessionState.ERROR:
                        self._session.stop()
                finally:
                    self._disconnect()
        finally:
            self._running.release()

    def _connect(self) -> None:
        try:
            self._receiver.open()
        except EventSubError:
            self._session.connection_changed(False)
            raise

    def _disconnect(self) -> None:
        try:
            self._receiver.close()
        finally:
            self._session.connection_changed(False)

    def _tick(self) -> None:
        try:
            for count in range(_MAX_MESSAGES_PER_TICK + 1):
                if not self._receiver.receive():
                    self._session.tick()
                    return
                if count == _MAX_MESSAGES_PER_TICK:
                    break
            logger.warning("EventSub message drain limit reached; interrupting collection")
            self._disconnect()
            raise EventSubError("EventSub message drain limit reached")
        except EventSubError:
            self._session.connection_changed(False)
            raise
