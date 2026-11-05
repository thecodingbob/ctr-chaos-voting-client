import logging
from random import Random

from chaos_voting.game_files import GameFiles
from chaos_voting.voting import VotingRound


def test_round_logs_lifecycle_without_user_data(caplog) -> None:
    now = [0.0]
    with caplog.at_level(logging.INFO, logger="chaos_voting.voting"):
        round_ = VotingRound(("a", "b"), 1, clock=lambda: now[0], random=Random(1))
        round_.open()
        round_.vote("private-user-id", "1")
        now[0] = 1
        round_.close()
    assert "Voting round prepared" in caplog.text
    assert "Voting round opened" in caplog.text
    assert "Voting round closed with 1 votes" in caplog.text
    assert "private-user-id" not in caplog.text


def test_game_logs_submission_without_claiming_activation(tmp_path, caplog) -> None:
    game = GameFiles(tmp_path)
    with caplog.at_level(logging.INFO, logger="chaos_voting.game_files"):
        game.submit("effect")
    assert "waiting for the game to consume it" in caplog.text
    assert "activated" not in caplog.text
