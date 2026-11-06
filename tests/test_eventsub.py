import io
import json
import logging
import traceback
from datetime import UTC, datetime
from urllib.error import HTTPError

import pytest

from chaos_voting.clock import Clock
from chaos_voting.eventsub import EventSubError, TwitchEventSub
from chaos_voting.game_files import GameFiles
from chaos_voting.session import Session, SessionState
from chaos_voting.voting import RoundState


def message(kind, payload=None, **metadata):
    return json.dumps({"metadata": {"message_type": kind, **metadata}, "payload": payload or {}})


WELCOME = message("session_welcome", {"session": {"id": "session", "keepalive_timeout_seconds": 10}})


class Socket:
    def __init__(self, *messages):
        self.messages = list(messages)
        self.closed = False

    def recv(self, *, timeout):
        if not self.messages:
            raise TimeoutError
        return self.messages.pop(0)

    def close(self):
        self.closed = True


def notification(event_id="event", text="1", user="private-user"):
    return message("notification", {"event": {"broadcaster_user_id": "123",
                   "chatter_user_id": user, "message": {"text": text}}},
                   message_id=event_id, message_timestamp="2026-11-06T10:00:00Z",
                   subscription_type="channel.chat.message")


def setup(*messages, response=None, clock=lambda: 0):
    socket = Socket(WELCOME, *messages)
    events, requests = [], []

    def subscribe(request, *, timeout):
        requests.append(request)
        return io.BytesIO(json.dumps(response or {"data": [{"status": "enabled"}]}).encode())

    receiver = TwitchEventSub("client", "secret-token", "123", on_connection=events.append,
                              on_message=lambda user, text, event_id, timestamp:
                              events.append((user, text, event_id, timestamp)),
                              connector=lambda *args, **kwargs: socket, opener=subscribe, clock=clock)
    return receiver, socket, events, requests


def test_subscribe_receive_and_disconnect(caplog):
    receiver, socket, events, requests = setup(notification(), notification())
    with caplog.at_level(logging.DEBUG):
        receiver.open()
        assert receiver.receive()
        assert receiver.receive()
        assert not receiver.receive()
        receiver.close()
    body = json.loads(requests[0].data)
    assert body["condition"] == {"broadcaster_user_id": "123", "user_id": "123"}
    assert body["transport"]["session_id"] == "session"
    event = ("private-user", "1", "event", datetime(2026, 11, 6, 10, tzinfo=UTC))
    assert events == [True, event, event, False]
    assert socket.closed
    assert "secret-token" not in caplog.text
    assert "private-user" not in caplog.text


@pytest.mark.parametrize("bad", [
    "not json", message("revocation"), message("session_reconnect"),
    notification(user=""), message("notification", {}, subscription_type="channel.chat.message"),
    message("notification", {"event": {"broadcaster_user_id": "123", "chatter_user_id": "u",
                                       "message": {"text": "1"}}}, message_id="id",
            message_timestamp="2026-11-06T10:00:00", subscription_type="channel.chat.message"),
])
def test_interruption_notifies_before_error(bad):
    receiver, socket, events, _ = setup(bad)
    receiver.open()
    with pytest.raises(EventSubError):
        receiver.receive()
    assert events == [True, False]
    assert socket.closed


def test_disconnect_callback_failure_is_reported_and_socket_closed():
    socket = Socket(WELCOME)

    def connection(connected):
        if not connected:
            raise RuntimeError("private payload")

    receiver = TwitchEventSub("client", "token", "123", on_connection=connection,
                              on_message=lambda *_: None, connector=lambda *a, **k: socket,
                              opener=lambda *a, **k: io.BytesIO(b'{"data":[{"status":"enabled"}]}'))
    receiver.open()
    with pytest.raises(EventSubError, match="disconnect callback failed") as error:
        receiver.close()
    assert "private payload" not in "".join(traceback.format_exception(error.value))
    assert socket.closed


def test_keepalive_timeout_disconnects():
    now = [0]
    receiver, _, events, _ = setup(message("session_keepalive"), clock=lambda: now[0])
    receiver.open()
    now[0] = 9
    receiver.receive()
    now[0] = 18
    assert not receiver.receive()
    now[0] = 19
    with pytest.raises(EventSubError):
        receiver.receive()
    assert events == [True, False]


def test_subscription_failure_does_not_report_connected():
    socket = Socket(WELCOME)
    events = []

    def fail(*args, **kwargs):
        raise HTTPError("url", 403, "secret", {}, None)

    receiver = TwitchEventSub("client", "token", "123", on_connection=events.append,
                              on_message=lambda *_: None, connector=lambda *a, **k: socket,
                              opener=fail)
    with pytest.raises(EventSubError):
        receiver.open()
    assert events == []
    assert socket.closed


def test_message_callback_failure_disconnects():
    socket = Socket(WELCOME, notification())
    events = []

    def fail(*args):
        raise RuntimeError("private payload")

    receiver = TwitchEventSub("client", "token", "123", on_connection=events.append,
                              on_message=fail, connector=lambda *a, **k: socket,
                              opener=lambda *a, **k: io.BytesIO(b'{"data":[{"status":"enabled"}]}'))
    receiver.open()
    with pytest.raises(EventSubError):
        receiver.receive()
    assert events == [True, False]
    assert socket.closed


def test_connection_callback_failure_closes_socket():
    socket = Socket(WELCOME)

    def fail(connected):
        raise RuntimeError("private")

    receiver = TwitchEventSub("client", "token", "123", on_connection=fail,
                              on_message=lambda *_: None, connector=lambda *a, **k: socket,
                              opener=lambda *a, **k: io.BytesIO(b'{"data":[{"status":"enabled"}]}'))
    with pytest.raises(EventSubError):
        receiver.open()
    assert socket.closed


def test_receiver_and_session_round_transition_and_disconnect(tmp_path):
    (tmp_path / "effects").write_text("one\ntwo\n", encoding="utf-8")
    now = [0.0]
    opened = datetime(2026, 11, 6, 10, tzinfo=UTC)

    class Platform:
        def announce_options(self, options):
            pass

        def announce_result(self, winner):
            pass

    class FakeClock(Clock):
        def monotonic(self):
            return now[0]

        def utc_now(self):
            return opened

    session = Session(GameFiles(tmp_path), Platform(), 10, clock=FakeClock())
    socket = Socket(WELCOME, notification(), notification(), notification(), message("revocation"))
    receiver = TwitchEventSub("client", "token", "123", on_connection=session.connection_changed,
                              on_message=session.vote_event, connector=lambda *a, **k: socket,
                              opener=lambda *a, **k: io.BytesIO(b'{"data":[{"status":"enabled"}]}'))
    session.start()
    receiver.open()
    session.tick()
    receiver.receive()
    receiver.receive()
    assert session.snapshot().round.counts == (1, 0)
    now[0] = 10
    session.tick()
    (tmp_path / "command").unlink()
    session.tick()
    receiver.receive()
    assert session.snapshot().round.counts == (0, 0)
    with pytest.raises(EventSubError):
        receiver.receive()
    assert session.snapshot().state is SessionState.WAITING_CONNECTION
    assert session.snapshot().round.state is RoundState.CANCELLED
