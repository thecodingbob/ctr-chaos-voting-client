from random import Random

import pytest

from chaos_voting.voting import RoundState, VotingRound


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_round(clock: Clock, effects: tuple[str, ...] = ("a", "b", "c")) -> VotingRound:
    return VotingRound(effects, 10, clock=clock, random=Random(1))


def test_options_are_distinct_and_limited(clock: Clock) -> None:
    round_ = make_round(clock, ("a", "a", "b", "c", "d"))
    assert len(set(round_.snapshot().options)) == 3
    assert len(make_round(clock, ("a", "b")).snapshot().options) == 2


@pytest.mark.parametrize("effects", [(), ("a",), ("a", "a")])
def test_insufficient_effects(clock: Clock, effects: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        make_round(clock, effects)


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_invalid_duration(clock: Clock, duration: float) -> None:
    with pytest.raises(ValueError):
        VotingRound(("a", "b"), duration, clock=clock)


def test_latest_valid_vote_and_exact_messages(clock: Clock) -> None:
    round_ = make_round(clock, ("a", "b"))
    assert round_.vote("user", "1")
    assert round_.vote("user", "2")
    for message in ("3", " 1", "1 ", "01", "1\n", ""):
        assert not round_.vote("user", message)
    assert not round_.vote("", "1")
    assert round_.snapshot().counts == (0, 1)
    clock.now = 10
    assert not round_.vote("another", "1")
    assert round_.close() == round_.snapshot().options[1]
    assert round_.close() == round_.snapshot().winner


def test_ties_and_no_votes_choose_only_eligible_options(clock: Clock) -> None:
    tied = make_round(clock)
    tied.vote("one", "1")
    tied.vote("two", "2")
    empty = make_round(clock)
    clock.now = 10
    assert tied.close() in tied.snapshot().options[:2]
    assert empty.close() in empty.snapshot().options


def test_close_before_deadline_and_cancellation(clock: Clock) -> None:
    round_ = make_round(clock)
    with pytest.raises(RuntimeError):
        round_.close()
    round_.cancel("Connection lost")
    assert round_.snapshot().state is RoundState.CANCELLED
    assert round_.snapshot().cancellation_reason == "Connection lost"
    assert not round_.vote("user", "1")
    with pytest.raises(RuntimeError):
        round_.close()
