import random

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


def setup_session():
    game, platform, now = Game(), Platform(), [0.0]
    session = Session(game, platform, 10, clock=lambda: now[0], random=random.Random(1))
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
