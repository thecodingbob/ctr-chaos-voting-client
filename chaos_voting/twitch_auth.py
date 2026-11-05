"""Twitch public-client device authorization, driven by the application.

The caller displays the verification URI and code, schedules polling, and
handles tokens in memory.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from http.client import HTTPException
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)
SCOPES = ("user:read:chat", "user:write:chat")
_DEVICE_URL = "https://id.twitch.tv/oauth2/device"
_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"


class AuthenticationError(Exception):
    """Authorization failed or Twitch returned an invalid response."""


class _TransientAuthenticationError(AuthenticationError):
    """A request may be retried without restarting authorization."""


class _SlowDown(Exception):
    """The authorization server requested a slower polling rate."""


@dataclass(frozen=True)
class DeviceAuthorization:
    user_code: str
    verification_uri: str
    expires_at: float
    interval: int
    device_code: str = field(repr=False)


@dataclass(frozen=True)
class TwitchCredentials:
    user_id: str
    login: str
    expires_in: int
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


class TwitchDeviceAuth:
    """One authorization attempt; calls must be serialized by the caller."""

    def __init__(self, client_id: str, *, opener: Callable = urlopen,
                 clock: Callable[[], float] = monotonic) -> None:
        if not client_id:
            raise ValueError("Twitch client ID is required")
        self._client_id = client_id
        self._opener = opener
        self._clock = clock
        self._authorization: DeviceAuthorization | None = None
        self._next_poll = 0.0
        self._interval = 0
        self._pending_tokens: tuple[str, str] | None = None

    def start(self) -> DeviceAuthorization:
        self._authorization = None
        self._pending_tokens = None
        payload = self._post(_DEVICE_URL, {"client_id": self._client_id, "scopes": " ".join(SCOPES)})
        if (not all(_nonempty(payload.get(key)) for key in
                    ("device_code", "user_code", "verification_uri"))
                or not _positive_int(payload.get("expires_in"))
                or not _positive_int(payload.get("interval"))):
            raise AuthenticationError("Invalid Twitch device authorization response")
        now = self._clock()
        authorization = DeviceAuthorization(
            user_code=payload["user_code"], verification_uri=payload["verification_uri"],
            expires_at=now + payload["expires_in"], interval=payload["interval"],
            device_code=payload["device_code"],
        )
        self._authorization = authorization
        self._next_poll = now + authorization.interval
        self._interval = authorization.interval
        logger.info("Twitch device authorization started")
        return authorization

    def poll(self) -> TwitchCredentials | None:
        """Return credentials on success, or None while waiting or retrying validation."""
        auth = self._authorization
        if auth is None and self._pending_tokens is None:
            raise AuthenticationError("No Twitch authorization is in progress")
        now = self._clock()
        if self._pending_tokens is not None:
            if now < self._next_poll:
                return None
            return self._validate_pending()
        assert auth is not None
        if now >= auth.expires_at:
            self._authorization = None
            logger.info("Twitch device authorization expired")
            raise AuthenticationError("Twitch device authorization expired")
        if now < self._next_poll:
            return None
        self._next_poll = now + self._interval
        try:
            payload = self._post(_TOKEN_URL, {
                "client_id": self._client_id, "scopes": " ".join(SCOPES),
                "device_code": auth.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }, pending_allowed=True)
        except _SlowDown:
            self._interval += 5
            self._next_poll = self._clock() + self._interval
            logger.info("Twitch requested slower device authorization polling")
            return None
        except AuthenticationError:
            self._authorization = None
            raise
        if payload is None:
            return None
        self._authorization = None  # Device codes are single-use, even if validation fails.
        if (not _nonempty(payload.get("access_token"))
                or not _nonempty(payload.get("refresh_token"))
                or str(payload.get("token_type", "")).lower() != "bearer"):
            raise AuthenticationError("Invalid Twitch token response")
        self._pending_tokens = (payload["access_token"], payload["refresh_token"])
        return self._validate_pending()

    def _validate_pending(self) -> TwitchCredentials | None:
        assert self._pending_tokens is not None
        token, refresh_token = self._pending_tokens
        try:
            validation = self._request(
                Request(_VALIDATE_URL, headers={"Authorization": f"OAuth {token}"})
            )
        except _TransientAuthenticationError:
            self._next_poll = self._clock() + self._interval
            logger.warning("Twitch token validation temporarily unavailable; will retry")
            return None
        except AuthenticationError:
            self._pending_tokens = None
            raise
        assert validation is not None
        scopes = validation.get("scopes")
        if (validation.get("client_id") != self._client_id
                or not _nonempty(validation.get("user_id"))
                or not _nonempty(validation.get("login"))
                or not _positive_int(validation.get("expires_in"))
                or not isinstance(scopes, list) or not all(scope in scopes for scope in SCOPES)):
            self._pending_tokens = None
            raise AuthenticationError("Twitch token validation failed or permissions are missing")
        self._pending_tokens = None
        logger.info("Twitch user authorization completed and token validated")
        return TwitchCredentials(user_id=validation["user_id"], login=validation["login"],
                                 expires_in=validation["expires_in"], access_token=token,
                                 refresh_token=refresh_token)

    def _post(self, url: str, data: dict[str, str], *, pending_allowed: bool = False) -> dict | None:
        request = Request(url, data=urlencode(data).encode("ascii"), method="POST")
        return self._request(request, pending_allowed=pending_allowed)

    def _request(self, request: Request, *, pending_allowed: bool = False) -> dict | None:
        try:
            with self._opener(request, timeout=10) as response:
                payload = json.load(response)
        except HTTPError as exc:
            if pending_allowed and exc.code == 400:
                try:
                    payload = json.load(exc)
                except (OSError, HTTPException, ValueError, UnicodeError):
                    payload = None
                if isinstance(payload, dict) and payload.get("message") == "authorization_pending":
                    return None
                if isinstance(payload, dict) and payload.get("message") == "slow_down":
                    raise _SlowDown from None
            logger.warning("Twitch authentication request failed with HTTP status %d", exc.code)
            if exc.code == 429 or 500 <= exc.code < 600:
                raise _TransientAuthenticationError("Twitch authentication temporarily unavailable") from None
            raise AuthenticationError(f"Twitch authentication failed (HTTP {exc.code})") from None
        except (URLError, OSError, HTTPException, ValueError, UnicodeError):
            logger.warning("Twitch authentication request failed or returned invalid data")
            raise _TransientAuthenticationError("Twitch authentication request failed or returned invalid data") from None
        if not isinstance(payload, dict):
            logger.warning("Twitch authentication returned invalid data")
            raise AuthenticationError("Invalid Twitch authentication response")
        return payload
