from pathlib import Path

import pytest

from chaos_voting.game_files import CommandPendingError, GameFileError, GameFiles


@pytest.fixture
def game(tmp_path: Path) -> GameFiles:
    directory = tmp_path / "chaos"
    directory.mkdir()
    return GameFiles(directory)


def test_catalog_is_read_again_each_time(game: GameFiles, tmp_path: Path) -> None:
    catalog = tmp_path / "chaos" / "effects"
    catalog.write_text("first\nsecond\n", encoding="utf-8")
    assert game.read_effects() == ("first", "second")
    catalog.write_text("", encoding="utf-8")
    assert game.read_effects() == ()


@pytest.mark.parametrize("content", [b"\xff", b"a\x00b", b"\xef\xbb\xbffirst", b"a\n\nb"])
def test_invalid_catalog(game: GameFiles, tmp_path: Path, content: bytes) -> None:
    (tmp_path / "chaos" / "effects").write_bytes(content)
    with pytest.raises(GameFileError):
        game.read_effects()


def test_missing_catalog(game: GameFiles) -> None:
    with pytest.raises(GameFileError):
        game.read_effects()


def test_submission_and_consumption(game: GameFiles, tmp_path: Path) -> None:
    command = tmp_path / "chaos" / "command"
    assert not game.command_pending()
    game.submit("fly-me-to-the-moon")
    assert command.read_bytes() == b"fly-me-to-the-moon"
    assert not (tmp_path / "chaos" / "command.tmp").exists()
    assert game.command_pending()
    with pytest.raises(CommandPendingError):
        game.submit("other")
    assert command.read_bytes() == b"fly-me-to-the-moon"
    command.unlink()
    assert not game.command_pending()


def test_existing_temporary_file_is_not_overwritten(game: GameFiles, tmp_path: Path) -> None:
    temporary = tmp_path / "chaos" / "command.tmp"
    temporary.write_bytes(b"existing")
    with pytest.raises(CommandPendingError):
        game.submit("effect")
    assert temporary.read_bytes() == b"existing"


@pytest.mark.parametrize("slug", ["", " spaced", "two words", "a\x00b", "x" * 128, "\ufeffa"])
def test_invalid_commands(game: GameFiles, slug: str) -> None:
    with pytest.raises(ValueError):
        game.submit(slug)
