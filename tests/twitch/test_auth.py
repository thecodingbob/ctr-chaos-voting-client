import io
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import pytest

from chaos_voting.twitch.auth import AuthenticationError, TwitchCredentials, TwitchDeviceAuth

DEVICE = {"device_code": "device-secret", "user_code": "ABC123", "expires_in": 30,
          "interval": 5, "verification_uri": "https://www.twitch.tv/activate"}
TOKEN = {"access_token": "access-secret", "refresh_token": "refresh-secret",
         "token_type": "bearer"}
VALIDATION = {"client_id": "client", "user_id": "123", "login": "channel",
              "scopes": ["user:read:chat", "user:write:chat"], "expires_in": 3600}


class TwitchServer:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode())


def pending():
    return HTTPError("https://id.twitch.tv/oauth2/token", 400, "bad", {},
                     io.BytesIO(b'{"status":400,"message":"authorization_pending"}'))


def test_device_flow_polling_and_validation(caplog):
    server = TwitchServer(DEVICE, pending(), TOKEN, VALIDATION)
    now = [0.0]
    auth = TwitchDeviceAuth("client", opener=server, clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger="chaos_voting.twitch.auth"):
        device = auth.start()
        assert auth.poll() is None
        now[0] = 5
        assert auth.poll() is None
        now[0] = 9
        assert auth.poll() is None
        now[0] = 10
        credentials = auth.poll()
    assert len(server.requests) == 4
    assert parse_qs(server.requests[0].data.decode())["scopes"] == [
        "user:read:chat user:write:chat"
    ]
    assert server.requests[3].get_header("Authorization") == "OAuth access-secret"
    assert credentials.user_id == "123"
    assert credentials.access_token == "access-secret"
    for secret in ("device-secret", "access-secret", "refresh-secret"):
        assert secret not in caplog.text
        assert secret not in repr(device)
        assert secret not in repr(credentials)
    with pytest.raises(AuthenticationError):
        auth.poll()


def test_expiry_and_invalid_device_response():
    now = [0]
    server = TwitchServer(DEVICE)
    auth = TwitchDeviceAuth("client", opener=server, clock=lambda: now[0])
    auth.start()
    now[0] = 30
    with pytest.raises(AuthenticationError, match="expired"):
        auth.poll()
    assert len(server.requests) == 1
    bad = TwitchDeviceAuth("client", opener=TwitchServer({**DEVICE, "interval": 0}))
    with pytest.raises(AuthenticationError):
        bad.start()


@pytest.mark.parametrize("change", [
    {"client_id": "other"}, {"user_id": ""}, {"scopes": ["user:read:chat"]},
    {"expires_in": 0},
])
def test_invalid_token_validation_fails_closed(change):
    now = [0]
    server = TwitchServer(DEVICE, TOKEN, {**VALIDATION, **change})
    auth = TwitchDeviceAuth("client", opener=server, clock=lambda: now[0])
    auth.start()
    now[0] = 5
    with pytest.raises(AuthenticationError):
        auth.poll()
    with pytest.raises(AuthenticationError, match="No Twitch authorization"):
        auth.poll()


def test_http_error_does_not_expose_response():
    error = HTTPError("secret-url", 400, "secret", {}, io.BytesIO(b'{"message":"secret"}'))
    now = [0]
    auth = TwitchDeviceAuth("client", opener=TwitchServer(DEVICE, error), clock=lambda: now[0])
    auth.start()
    now[0] = 5
    with pytest.raises(AuthenticationError) as exc:
        auth.poll()
    assert "secret" not in str(exc.value)


def test_validation_retry_keeps_tokens_without_reusing_device_code(caplog):
    now = [0]
    server = TwitchServer(DEVICE, TOKEN, URLError("secret"), VALIDATION)
    auth = TwitchDeviceAuth("client", opener=server, clock=lambda: now[0])
    auth.start()
    now[0] = 5
    with caplog.at_level(logging.WARNING, logger="chaos_voting.twitch.auth"):
        assert auth.poll() is None
        now[0] = 9
        assert auth.poll() is None
        now[0] = 10
        credentials = auth.poll()
    assert credentials.user_id == "123"
    assert [request.full_url for request in server.requests].count(
        "https://id.twitch.tv/oauth2/token"
    ) == 1
    assert "secret" not in caplog.text


def test_slow_down_increases_subsequent_poll_interval():
    now = [0]
    slowdown = HTTPError("url", 400, "bad", {}, io.BytesIO(b'{"message":"slow_down"}'))
    server = TwitchServer(DEVICE, slowdown, pending(), TOKEN, VALIDATION)
    auth = TwitchDeviceAuth("client", opener=server, clock=lambda: now[0])
    auth.start()
    now[0] = 5
    assert auth.poll() is None
    now[0] = 14
    assert auth.poll() is None
    assert len(server.requests) == 2
    now[0] = 15
    assert auth.poll() is None
    now[0] = 24
    assert auth.poll() is None
    assert len(server.requests) == 3
    now[0] = 25
    assert auth.poll().user_id == "123"


def test_refresh_rotates_tokens_and_retries_validation_only(caplog):
    now = [0]
    credentials = TwitchCredentials("123", "channel", 5, "old-access", "old/refresh+token")
    server = TwitchServer(TOKEN, URLError("secret"), VALIDATION)
    auth = TwitchDeviceAuth("client", opener=server, clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger="chaos_voting.twitch.auth"):
        assert auth.refresh(credentials) is None
        assert auth.poll() is None
        with pytest.raises(AuthenticationError, match="validation is in progress"):
            auth.start()
        now[0] = 5
        refreshed = auth.poll()
    assert refreshed.access_token == "access-secret"
    assert refreshed.refresh_token == "refresh-secret"
    assert parse_qs(server.requests[0].data.decode()) == {
        "client_id": ["client"], "grant_type": ["refresh_token"],
        "refresh_token": ["old/refresh+token"],
    }
    assert len(server.requests) == 3
    for secret in ("old-access", "old/refresh+token", "access-secret", "refresh-secret"):
        assert secret not in caplog.text
    with pytest.raises(AuthenticationError, match="already been used"):
        auth.refresh(credentials)
    assert len(server.requests) == 3


def test_refresh_rejects_different_account():
    credentials = TwitchCredentials("123", "channel", 5, "old", "refresh")
    server = TwitchServer(TOKEN, {**VALIDATION, "user_id": "different"})
    auth = TwitchDeviceAuth("client", opener=server)
    with pytest.raises(AuthenticationError, match="validation failed"):
        auth.refresh(credentials)
    with pytest.raises(AuthenticationError, match="No Twitch authorization"):
        auth.poll()


def test_ambiguous_refresh_failure_is_not_retried():
    credentials = TwitchCredentials("123", "channel", 5, "old", "secret-refresh")
    server = TwitchServer(URLError("secret-refresh"))
    auth = TwitchDeviceAuth("client", opener=server)
    with pytest.raises(AuthenticationError, match="do not retry") as exc:
        auth.refresh(credentials)
    assert "secret-refresh" not in str(exc.value)
    assert len(server.requests) == 1
    with pytest.raises(AuthenticationError, match="already been used"):
        auth.refresh(credentials)
    assert len(server.requests) == 1


@pytest.mark.parametrize("response", [
    [], HTTPError("url", 408, "timeout", {}, None),
])
def test_other_uncertain_refresh_responses_are_not_retried(response):
    credentials = TwitchCredentials("123", "channel", 5, "old", "refresh")
    server = TwitchServer(response)
    auth = TwitchDeviceAuth("client", opener=server)
    with pytest.raises(AuthenticationError, match="do not retry"):
        auth.refresh(credentials)
    with pytest.raises(AuthenticationError, match="already been used"):
        auth.refresh(credentials)
    assert len(server.requests) == 1
