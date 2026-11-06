from datetime import UTC, datetime

import pytest

from chaos_voting.runner import SessionRunner
from chaos_voting.twitch.eventsub import EventSubError


class Session:
    def __init__(self):
        self.events = []

    def connection_changed(self, connected):
        self.events.append(("connection", connected))

    def vote_event(self, *args):
        self.events.append(("vote", *args))

    def tick(self):
        self.events.append(("tick",))


class Receiver:
    def __init__(self, client_id, token, user_id, *, on_connection, on_message):
        self.on_connection = on_connection
        self.on_message = on_message
        self.messages = []
        self.closed = False

    def open(self):
        self.on_connection(True)

    def close(self):
        self.closed = True
        self.on_connection(False)

    def receive(self):
        if not self.messages:
            return False
        item = self.messages.pop(0)
        if isinstance(item, Exception):
            raise item
        self.on_message(*item)
        return True


def setup():
    session = Session()
    receivers = []

    def factory(*args, **kwargs):
        receiver = Receiver(*args, **kwargs)
        receivers.append(receiver)
        return receiver

    runner = SessionRunner(session, "client", "token", "user", receiver_factory=factory)
    return runner, session, receivers[0]


def test_drains_before_tick():
    coordinator, session, receiver = setup()
    coordinator._connect()
    message = ("voter", "1", "id", datetime.now(UTC))
    receiver.messages.append(message)
    coordinator._tick()
    assert session.events == [("connection", True), ("vote", *message), ("tick",)]


def test_receive_failure_reports_interruption_before_any_tick():
    coordinator, session, receiver = setup()
    coordinator._connect()
    receiver.messages.append(EventSubError("interrupted"))
    with pytest.raises(EventSubError):
        coordinator._tick()
    assert session.events[-1] == ("connection", False)
    assert ("tick",) not in session.events


def test_drain_limit_disconnects_without_tick(monkeypatch):
    monkeypatch.setattr("chaos_voting.runner._MAX_MESSAGES_PER_TICK", 1)
    coordinator, session, receiver = setup()
    coordinator._connect()
    receiver.messages.append(("voter", "1", "id", datetime.now(UTC)))
    receiver.messages.append(("voter", "2", "id2", datetime.now(UTC)))
    with pytest.raises(EventSubError, match="drain limit"):
        coordinator._tick()
    assert receiver.closed
    assert session.events[-1] == ("connection", False)
    assert ("tick",) not in session.events


def test_exactly_drain_limit_is_allowed(monkeypatch):
    monkeypatch.setattr("chaos_voting.runner._MAX_MESSAGES_PER_TICK", 1)
    coordinator, session, receiver = setup()
    coordinator._connect()
    receiver.messages.append(("voter", "1", "id", datetime.now(UTC)))
    coordinator._tick()
    assert not receiver.closed
    assert session.events[-1] == ("tick",)
