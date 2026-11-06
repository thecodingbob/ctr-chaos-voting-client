import io
import json
import logging
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError

import pytest

from chaos_voting.platform import PlatformError
from chaos_voting.twitch import TwitchPlatform


class Response(io.BytesIO):
    pass


def test_announcements_use_authenticated_channel_and_number_options(caplog):
    requests = []

    def open_request(request, *, timeout):
        requests.append((request, timeout))
        return Response(b'{"data":[{"is_sent":true}]}')

    platform = TwitchPlatform("client", "secret-token", "123", opener=open_request)
    with caplog.at_level(logging.INFO, logger="chaos_voting.twitch"):
        platform.announce_options(("first", "second"))
        platform.announce_result("first")
    request, timeout = requests[0]
    assert timeout == 10
    assert request.full_url == "https://api.twitch.tv/helix/chat/messages"
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert json.loads(request.data) == {
        "broadcaster_id": "123", "sender_id": "123",
        "message": "Vote with a number: 1: first | 2: second",
    }
    assert "3:" not in json.loads(request.data)["message"]
    assert json.loads(requests[1][0].data)["message"] == "Voting closed. Winner: first"
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize("body", [
    b'{"data":[{"is_sent":false}]}', b'{"data":[]}', b'{}', b'[]', b'not json',
])
def test_unconfirmed_or_invalid_responses_fail(body):
    platform = TwitchPlatform("client", "token", "123", opener=lambda *a, **k: Response(body))
    with pytest.raises(PlatformError):
        platform.announce_result("effect")


@pytest.mark.parametrize("error", [
    HTTPError("https://example.com/secret-token", 429, "secret-token", {}, None),
    URLError("secret-token"),
])
def test_transport_errors_do_not_leak_secrets_or_retry(error, caplog):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise error

    platform = TwitchPlatform("client", "secret-token", "123", opener=fail)
    with caplog.at_level(logging.WARNING, logger="chaos_voting.twitch"), \
            pytest.raises(PlatformError) as exc:
        platform.announce_result("effect")
    assert len(calls) == 1
    assert "secret-token" not in caplog.text
    assert "secret-token" not in str(exc.value)


def test_long_announcement_fails_without_sending():
    platform = TwitchPlatform("client", "token", "123", opener=lambda *a, **k: pytest.fail())
    with pytest.raises(PlatformError):
        platform.announce_options(("a" * 490, "b"))


def test_incomplete_response_is_a_platform_error():
    class TruncatedResponse(Response):
        def read(self, *args):
            raise IncompleteRead(b"partial", 10)

    platform = TwitchPlatform(
        "client", "token", "123", opener=lambda *a, **k: TruncatedResponse()
    )
    with pytest.raises(PlatformError):
        platform.announce_result("effect")
