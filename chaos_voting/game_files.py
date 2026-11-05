"""File adapter for the game's external Chaos provider."""

from pathlib import Path


class GameFileError(Exception):
    """The game's files could not be read or updated safely."""


class CommandPendingError(GameFileError):
    """A command is already waiting for the game to consume it."""


def _valid_slug(slug: str) -> bool:
    try:
        encoded = slug.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        slug != ""
        and slug == slug.strip()
        and len(encoded) < 128
        and not any(character.isspace() or ord(character) < 32 for character in slug)
        and "\ufeff" not in slug
    )


class GameFiles:
    """Read effects and submit one command at a time to the game."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    def read_effects(self) -> tuple[str, ...]:
        """Read the current catalog; an empty file represents an empty catalog."""
        try:
            content = (self._directory / "effects").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise GameFileError("Cannot read the effects catalog") from exc
        effects = content.splitlines()
        for effect in effects:
            if not _valid_slug(effect):
                raise GameFileError(f"The effects catalog contains an invalid slug: {effect}")
        return tuple(effects)

    def command_pending(self) -> bool:
        """Return whether a command exists, not whether an effect was activated."""
        try:
            (self._directory / "command").stat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise GameFileError("Cannot check the pending command") from exc
        return True

    def submit(self, slug: str) -> None:
        """Publish a command using the protocol's single-producer assumption.

        The caller must serialize submissions and revalidate the winner against
        the current catalog immediately before submission.
        """
        if not _valid_slug(slug):
            raise ValueError("Invalid effect slug")
        if self.command_pending():
            raise CommandPendingError("A command is already pending")
        temporary = self._directory / "command.tmp"
        command = self._directory / "command"
        created = False
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                created = True
                stream.write(slug)
            temporary.rename(command)
        except FileExistsError as exc:
            raise CommandPendingError("A command or temporary file already exists") from exc
        except OSError as exc:
            raise GameFileError("Cannot submit the command") from exc
        finally:
            if created:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
