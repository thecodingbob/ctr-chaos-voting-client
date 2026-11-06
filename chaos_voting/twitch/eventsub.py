"""Explicitly driven Twitch EventSub chat receiver.

All methods and callbacks must run on the same thread as the voting session.
This receiver does not establish trustworthy voting-window boundaries: do not
connect it to a live voting session until event timing and scheduling are
integrated. A delayed caller may conservatively trigger a keepalive failure.
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime
from http.client import HTTPException
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

logger = logging.getLogger(__name__)
_socket_logger = logging.getLogger("chaos_voting.twitch.eventsub.websocket")
_socket_logger.addHandler(logging.NullHandler())
_socket_logger.propagate = False
_socket_logger.setLevel(logging.CRITICAL + 1)
_URL = "wss://eventsub.wss.twitch.tv/ws"
_SUBSCRIPTIONS_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"
_WELCOME_TIMEOUT = 10
_SUBSCRIPTION_TYPE = "channel.chat.message"


class EventSubError(Exception):
    """EventSub is unavailable or message collection is no longer reliable."""


class TwitchEventSub:
    """Receive chat events with a user token; reconnection is caller-driven.

    Reconnect requests are treated conservatively as interruptions. A new socket
    and subscription are required; no votes are preserved across interruptions.
    """

    def __init__(
        self, client_id: str, access_token: str, user_id: str, *,
        on_connection: Callable[[bool], None],
        on_message: Callable[[str, str, str, datetime], object],
        connector: Callable = connect, opener: Callable = urlopen,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not all((client_id, access_token, user_id)):
            raise ValueError("Twitch client ID, user token and user ID are required")
        self._client_id = client_id
        self._token = access_token
        self._user_id = user_id
        self._on_connection = on_connection
        self._on_message = on_message
        self._connector = connector
        self._opener = opener
        self._clock = clock
        self._socket = None
        self._connected = False
        self._deadline = 0.0
        self._keepalive = 0

    def open(self) -> None:
        """Connect and subscribe before reporting a usable chat connection."""
        if self._socket is not None:
            raise EventSubError("EventSub connection is already open")
        try:
            self._socket = self._connector(_URL, open_timeout=_WELCOME_TIMEOUT,
                                           ping_interval=None, logger=_socket_logger)
            message = self._decode(self._socket.recv(timeout=_WELCOME_TIMEOUT))
            if message["metadata"].get("message_type") != "session_welcome":
                raise EventSubError("Expected an EventSub welcome message")
            session = message["payload"].get("session")
            if not isinstance(session, dict):
                raise EventSubError("Invalid EventSub welcome message")
            session_id = session.get("id")
            keepalive = session.get("keepalive_timeout_seconds")
            if not isinstance(session_id, str) or not session_id or type(keepalive) is not int or keepalive <= 0:
                raise EventSubError("Invalid EventSub welcome message")
            self._keepalive = keepalive
            self._deadline = self._clock() + keepalive
            self._subscribe(session_id)
            self._connected = True
            try:
                self._on_connection(True)
            except Exception:  # noqa: BLE001 - callbacks are an external boundary
                raise EventSubError("EventSub connection callback failed") from None
            logger.info("Twitch EventSub chat subscription connected")
        except (EventSubError, OSError, WebSocketException, ValueError):
            self.close()
            logger.warning("Could not establish Twitch EventSub chat subscription")
            raise EventSubError("Could not establish Twitch EventSub chat subscription") from None

    def receive(self, timeout: float = 0) -> bool:
        """Process one message; return False when no message is available.

        Call repeatedly until False before advancing the session. A connection
        error closes the socket and notifies the caller before raising.
        """
        if not self._connected or self._socket is None:
            raise EventSubError("EventSub is not connected")
        if timeout < 0:
            raise ValueError("Timeout must be non-negative")
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            self._disconnect("Twitch EventSub keepalive timed out")
        try:
            raw = self._socket.recv(timeout=min(timeout, remaining))
        except TimeoutError:
            if self._clock() >= self._deadline:
                self._disconnect("Twitch EventSub keepalive timed out")
            return False
        except (OSError, WebSocketException):
            self._disconnect("Twitch EventSub connection lost")
        try:
            message = self._decode(raw)
            self._handle(message)
        except EventSubError:
            self._disconnect("Twitch EventSub message collection interrupted")
        return True

    def close(self) -> None:
        socket, self._socket = self._socket, None
        was_connected, self._connected = self._connected, False
        callback_failed = False
        try:
            if was_connected:
                try:
                    self._on_connection(False)
                except Exception:  # noqa: BLE001 - still close the transport
                    logger.error("Twitch EventSub disconnect callback failed")
                    callback_failed = True
                logger.info("Twitch EventSub chat disconnected")
        finally:
            if socket is not None:
                socket.close()
        if callback_failed:
            raise EventSubError("EventSub disconnect callback failed") from None

    def _disconnect(self, message: str) -> None:
        logger.warning("%s", message)
        try:
            self.close()
        except EventSubError:
            raise EventSubError("EventSub disconnect callback failed") from None
        raise EventSubError(message) from None

    def _subscribe(self, session_id: str) -> None:
        body = json.dumps({"type": _SUBSCRIPTION_TYPE, "version": "1",
                           "condition": {"broadcaster_user_id": self._user_id,
                                         "user_id": self._user_id},
                           "transport": {"method": "websocket", "session_id": session_id}})
        request = Request(_SUBSCRIPTIONS_URL, data=body.encode("utf-8"), method="POST", headers={
            "Authorization": f"Bearer {self._token}", "Client-Id": self._client_id,
            "Content-Type": "application/json",
        })
        try:
            with self._opener(request, timeout=10) as response:
                result = json.load(response)
        except (HTTPError, URLError, OSError, HTTPException, ValueError, UnicodeError):
            raise EventSubError("Twitch EventSub subscription request failed") from None
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise EventSubError("Invalid Twitch EventSub subscription response")
        data = result["data"]
        if len(data) != 1 or not isinstance(data[0], dict) or data[0].get("status") != "enabled":
            raise EventSubError("Twitch EventSub subscription is not enabled")

    @staticmethod
    def _decode(raw: str | bytes) -> dict:
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeError, TypeError):
            raise EventSubError("Invalid EventSub message") from None
        if (not isinstance(message, dict) or not isinstance(message.get("metadata"), dict)
                or not isinstance(message.get("payload"), dict)):
            raise EventSubError("Invalid EventSub message")
        return message

    def _handle(self, message: dict) -> None:
        metadata, payload = message["metadata"], message["payload"]
        kind = metadata.get("message_type")
        if kind in ("session_keepalive", "notification"):
            self._deadline = self._clock() + self._keepalive
        if kind == "session_keepalive":
            return
        if kind in ("session_reconnect", "revocation"):
            raise EventSubError("EventSub subscription interrupted")
        if kind != "notification" or metadata.get("subscription_type") != _SUBSCRIPTION_TYPE:
            raise EventSubError("Unexpected EventSub message")
        event = payload.get("event")
        if not isinstance(event, dict) or not isinstance(event.get("message"), dict):
            raise EventSubError("Invalid EventSub chat event")
        event_id = metadata.get("message_id")
        timestamp = metadata.get("message_timestamp")
        user_id, text = event.get("chatter_user_id"), event["message"].get("text")
        if (not isinstance(event_id, str) or not event_id or not isinstance(user_id, str)
                or not user_id or not isinstance(text, str) or not isinstance(timestamp, str)
                or event.get("broadcaster_user_id") != self._user_id):
            raise EventSubError("Invalid EventSub chat event")
        try:
            sent_at = datetime.fromisoformat(timestamp)
            if sent_at.tzinfo is None or sent_at.utcoffset() is None:
                raise ValueError("Missing timestamp timezone")
        except ValueError:
            raise EventSubError("Invalid EventSub notification timestamp") from None
        try:
            self._on_message(user_id, text, event_id, sent_at)
        except Exception:  # noqa: BLE001 - callbacks are an external boundary
            raise EventSubError("EventSub message callback failed") from None
