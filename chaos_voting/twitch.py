"""Twitch chat announcements through the Helix Send Chat Message API.

The caller supplies an authenticated user token with the required permissions.
Receiving messages, token management, and connection monitoring are separate.
"""

import json
import logging
from collections.abc import Callable
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from chaos_voting.platform import PlatformError, VotingPlatform

logger = logging.getLogger(__name__)
_CHAT_URL = "https://api.twitch.tv/helix/chat/messages"


class TwitchPlatform(VotingPlatform):
    """Send round announcements as the authenticated channel owner.

    No automatic retries: an HTTP failure can have an ambiguous outcome.
    """

    def __init__(
        self, client_id: str, access_token: str, user_id: str, *,
        opener: Callable = urlopen,
    ) -> None:
        if not all((client_id, access_token, user_id)):
            raise ValueError("Twitch client ID, user token and user ID are required")
        self._client_id = client_id
        self._access_token = access_token
        self._user_id = user_id
        self._opener = opener

    def announce_options(self, options: tuple[str, ...]) -> None:
        if not 2 <= len(options) <= 3:
            raise ValueError("A round must have two or three options")
        choices = " | ".join(f"{number}: {option}" for number, option in enumerate(options, 1))
        self._send(f"Vote with a number: {choices}")
        logger.info("Twitch voting options sent")

    def announce_result(self, winner: str) -> None:
        self._send(f"Voting closed. Winner: {winner}")
        logger.info("Twitch voting result sent")

    def _send(self, message: str) -> None:
        if len(message) > 500:
            logger.warning("Twitch announcement exceeds the message length limit")
            raise PlatformError("Twitch announcement exceeds the message length limit")
        body = json.dumps({
            "broadcaster_id": self._user_id,
            "sender_id": self._user_id,
            "message": message,
        }).encode("utf-8")
        request = Request(_CHAT_URL, data=body, headers={
            "Authorization": f"Bearer {self._access_token}",
            "Client-Id": self._client_id,
            "Content-Type": "application/json",
        }, method="POST")
        try:
            with self._opener(request, timeout=10) as response:
                payload = json.load(response)
        except HTTPError as exc:
            logger.warning("Twitch chat request failed with HTTP status %d", exc.code)
            raise PlatformError(f"Twitch chat request failed (HTTP {exc.code})") from None
        except (URLError, OSError, HTTPException, ValueError, UnicodeError):
            logger.warning("Twitch chat request failed or returned an invalid response")
            raise PlatformError("Twitch chat request failed or returned an invalid response") from None
        if not isinstance(payload, dict):
            raise PlatformError("Twitch chat returned an invalid response")
        data = payload.get("data")
        if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict)
                or data[0].get("is_sent") is not True):
            logger.warning("Twitch chat did not confirm message delivery")
            raise PlatformError("Twitch chat did not confirm message delivery")
