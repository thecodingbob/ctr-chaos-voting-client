import random
from datetime import UTC, datetime, timedelta, timezone

import pytest

from chaos_voting.clock import Clock
from chaos_voting.game_files import GameFileError
from chaos_voting.platform import PlatformError
from chaos_voting.session import Session, SessionState
from chaos_voting.voting import RoundState


class Game:
    def __init__(self):
        self.effects = ["one", "two", "three"]
        self.pending = False
        self.submissions = []
        self.error = False

    def read_effects(self):
        if self.error:
            raise GameFileError("Catalog unavailable")
        return self.effects

    def command_pending(self):
        return self.pending

    def submit(self, slug):
        self.submissions.append(slug)
        self.pending = True


class Platform:
    def __init__(self):
        self.options = []
        self.results = []
        self.fail_result = False

    def announce_options(self, options):
        self.options.append(options)

    def announce_result(self, winner):
        self.results.append(winner)
        if self.fail_result:
            raise PlatformError("ambiguous failure")


class FakeClock(Clock):
    def __init__(self, now, utc=None):
        self.now = now
        self.utc = utc or datetime(2026, 11, 6, 10, tzinfo=UTC)

    def monotonic(self):
        return self.now[0]

    def utc_now(self):
        return self.utc


def setup_session():
    game, platform, now = Game(), Platform(), [0.0]
    session = Session(game, platform, 10, clock=FakeClock(now), random=random.Random(1))
    session.connection_changed(True)
    session.start()
    return session, game, platform, now


def test_complete_cycle_waits_for_consumption():
    session, game, platform, now = setup_session()
    session.tick()
    assert session.vote("user", "1")
    now[0] = 10
    session.tick()
    assert game.submissions == [platform.options[0][0]]
    assert platform.results == game.submissions
    session.tick()
    assert session.snapshot().state is SessionState.WAITING_COMMAND
    assert len(platform.options) == 1
    game.pending = False
    session.tick()
    assert len(platform.options) == 2


def test_pending_at_start_and_stop_preserves_command():
    session, game, platform, _ = setup_session()
    game.pending = True
    session.tick()
    session.stop()
    assert game.pending
    session.start()
    session.tick()
    assert session.snapshot().state is SessionState.WAITING_COMMAND
    assert not platform.options


def test_disconnect_cancels_even_if_reconnected_before_tick():
    session, game, platform, now = setup_session()
    session.tick()
    session.connection_changed(False)
    assert session.snapshot().round.state is RoundState.CANCELLED
    session.connection_changed(True)
    now[0] = 20
    session.tick()
    assert not game.submissions
    assert not platform.results
    assert len(platform.options) == 2


def test_missing_winner_still_submitted():
    session, game, platform, now = setup_session()
    session.tick()
    session.vote("user", "1")
    game.effects.remove(platform.options[0][0])
    now[0] = 10
    session.tick()
    assert game.submissions == [platform.options[0][0]]
    assert session.snapshot().state is SessionState.WAITING_COMMAND


def test_catalog_failure_during_vote_does_not_block_submission():
    session, game, platform, now = setup_session()
    session.tick()
    session.vote("user", "1")
    game.error = True
    now[0] = 10
    session.tick()
    assert game.submissions == platform.results
    assert session.snapshot().state is SessionState.WAITING_COMMAND
    game.pending = False
    session.tick()
    assert session.snapshot().state is SessionState.WAITING_CATALOG
    assert len(platform.results) == 1


def test_catalog_retry_and_two_options():
    session, game, platform, _ = setup_session()
    game.error = True
    session.tick()
    assert session.snapshot().state is SessionState.WAITING_CATALOG
    game.error = False
    game.effects = ["one"]
    session.tick()
    assert not platform.options
    game.effects.append("two")
    session.tick()
    assert len(platform.options[0]) == 2
    assert not session.vote("user", "3")


def test_ambiguous_result_failure_is_not_retried():
    session, game, platform, now = setup_session()
    session.tick()
    platform.fail_result = True
    now[0] = 10
    session.tick()
    session.tick()
    assert session.snapshot().state is SessionState.ERROR
    assert len(platform.results) == 1
    assert not game.submissions


def test_event_timestamps_and_deduplication_across_rounds():
    now = [0.0]
    opened = datetime(2026, 11, 6, 10, tzinfo=UTC)
    game = Game()
    session = Session(game, Platform(), 10, clock=FakeClock(now, opened))
    session.connection_changed(True)
    session.start()
    session.tick()
    local_tz = timezone(timedelta(hours=1))
    assert not session.vote_event("u", "1", "before", opened - timedelta(microseconds=1))
    assert session.vote_event("u", "1", "one", opened.astimezone(local_tz))
    assert not session.vote_event("u", "2", "one", opened)
    assert session.snapshot().round.counts == (1, 0, 0)
    now[0] = 10
    session.tick()
    game.pending = False
    session.tick()
    assert not session.vote_event("u", "2", "one", opened)
    assert session.vote_event("u", "2", "two", opened)
    assert session.snapshot().round.counts == (0, 1, 0)


def test_event_requires_timezone_and_respects_monotonic_deadline():
    now = [0.0]
    opened = datetime(2026, 11, 6, 10, tzinfo=UTC)
    session = Session(Game(), Platform(), 10, clock=FakeClock(now, opened))
    session.connection_changed(True)
    session.start()
    session.tick()
    with pytest.raises(ValueError):
        session.vote_event("user", "1", "id", opened.replace(tzinfo=None))
    now[0] = 10
    assert not session.vote_event("user", "1", "id", opened)


def test_deduplication_capacity_cancels_round(monkeypatch):
    monkeypatch.setattr("chaos_voting.session._MAX_ROUND_MESSAGE_IDS", 1)
    opened = datetime(2026, 11, 6, 10, tzinfo=UTC)
    game = Game()
    session = Session(game, Platform(), 10, clock=FakeClock([0.0], opened))
    session.connection_changed(True)
    session.start()
    session.tick()
    assert session.vote_event("u", "1", "first", opened)
    assert not session.vote_event("u", "2", "first", opened)
    assert not session.vote_event("u", "2", "second", opened)
    assert session.snapshot().state is SessionState.ERROR
    assert session.snapshot().round.state is RoundState.CANCELLED
    assert not game.submissions


def test_late_event_does_not_exhaust_capacity(monkeypatch):
    monkeypatch.setattr("chaos_voting.session._MAX_ROUND_MESSAGE_IDS", 1)
    now = [0.0]
    opened = datetime(2026, 11, 6, 10, tzinfo=UTC)
    game = Game()
    session = Session(game, Platform(), 10, clock=FakeClock(now, opened))
    session.connection_changed(True)
    session.start()
    session.tick()
    assert session.vote_event("u", "1", "first", opened)
    now[0] = 10
    assert not session.vote_event("u", "2", "late", opened)
    session.tick()
    assert session.snapshot().state is SessionState.WAITING_COMMAND
    assert len(game.submissions) == 1


def test_wall_clock_changes_do_not_change_round_deadline():
    clock = FakeClock([0.0])
    game = Game()
    session = Session(game, Platform(), 10, clock=clock)
    session.connection_changed(True)
    session.start()
    session.tick()
    clock.utc += timedelta(days=1)
    session.tick()
    assert session.snapshot().state is SessionState.VOTING
    clock.now[0] = 10
    session.tick()
    assert session.snapshot().state is SessionState.WAITING_COMMAND
