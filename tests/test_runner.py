from threading import Event
from types import SimpleNamespace

import pytest

from chaos_voting.runner import SessionRunner
from chaos_voting.session import SessionState
from chaos_voting.twitch.eventsub import EventSubError


class Session:
    def __init__(self, calls, stop=None):
        self.calls = calls
        self.state = SessionState.STOPPED
        self.stop_event = stop

    def start(self):
        self.calls.append("start")
        self.state = SessionState.WAITING_CONNECTION

    def stop(self):
        self.calls.append("stop")
        self.state = SessionState.STOPPED

    def snapshot(self):
        return SimpleNamespace(state=self.state)

    def connection_changed(self, connected):
        pass

    def vote_event(self, *args):
        pass

    def tick(self):
        self.calls.append("tick")
        if self.stop_event:
            self.stop_event.set()


class Receiver:
    def __init__(self, calls):
        self.calls = calls
        self.fail_connect = False
        self.fail_receive = False

    def open(self):
        self.calls.append("connect")
        if self.fail_connect:
            self.fail_connect = False
            raise EventSubError("failed")

    def receive(self):
        if self.fail_receive:
            self.fail_receive = False
            raise EventSubError("failed")
        return False

    def close(self):
        self.calls.append("disconnect")


def runner_for(session, receiver, **kwargs):
    return SessionRunner(session, "client", "token", "user",
                         receiver_factory=lambda *args, **kw: receiver, **kwargs)


def test_retries_connection_and_collection_interruptions():
    calls, stop = [], Event()
    session = Session(calls, stop)
    receiver = Receiver(calls)
    receiver.fail_connect = receiver.fail_receive = True
    runner = runner_for(session, receiver, tick_interval=0.001, reconnect_interval=0.001)
    runner.run(stop)
    assert calls == ["start", "connect", "connect", "connect", "tick", "stop", "disconnect"]


def test_already_stopped_does_not_start():
    calls, stop = [], Event()
    stop.set()
    session = Session(calls)
    runner_for(session, Receiver(calls)).run(stop)
    assert not calls


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
def test_invalid_intervals(interval):
    session = Session([])
    with pytest.raises(ValueError):
        runner_for(session, Receiver([]), tick_interval=interval)


def test_terminal_error_is_preserved():
    calls, stop = [], Event()
    session = Session(calls)
    receiver = Receiver(calls)

    def fail_tick():
        calls.append("tick")
        session.state = SessionState.ERROR

    session.tick = fail_tick
    runner_for(session, receiver).run(stop)
    assert calls == ["start", "connect", "tick", "disconnect"]
    assert session.state is SessionState.ERROR


def test_terminal_error_during_receive_is_not_hidden_by_disconnect():
    calls, stop = [], Event()
    session = Session(calls)
    receiver = Receiver(calls)

    def fail_receive():
        session.state = SessionState.ERROR
        raise EventSubError("interrupted")

    receiver.receive = fail_receive
    runner_for(session, receiver).run(stop)
    assert calls == ["start", "connect", "disconnect"]
    assert session.state is SessionState.ERROR


def test_start_failure_runs_cleanup():
    calls, stop = [], Event()
    session = Session(calls)

    def fail_start():
        calls.append("start")
        raise RuntimeError("startup failed")

    session.start = fail_start
    with pytest.raises(RuntimeError, match="startup failed"):
        runner_for(session, Receiver(calls)).run(stop)
    assert calls == ["start", "stop", "disconnect"]
