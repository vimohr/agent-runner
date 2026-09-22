"""Per-project agent command configuration."""

from dataclasses import dataclass
from importlib import resources
import json
from pathlib import Path
import shlex
from typing import Mapping, Sequence


CONFIG_FILENAME = "agent-run.json"
DEFAULT_CONFIG_FILENAME = "default-agent-run.json"


def default_config_text() -> str:
    return (
        resources.files("agent_runner")
        .joinpath(DEFAULT_CONFIG_FILENAME)
        .read_text(encoding="utf-8")
    )


class ConfigurationError(ValueError):
    """Raised when a project's agent command configuration is unusable."""


@dataclass(frozen=True)
class AgentCommands:
    researcher: tuple[str, ...]
    supervisor: tuple[str, ...]


def _parse_command(name: str, value: object) -> tuple[str, ...]:
    command: Sequence[object]
    if isinstance(value, str):
        try:
            command = shlex.split(value)
        except ValueError as error:
            raise ConfigurationError(
                f"{name!r} is not a valid command: {error}"
            ) from error
    elif isinstance(value, list):
        command = value
    else:
        raise ConfigurationError(
            f"{name!r} must be a command string or an array of arguments"
        )

    if not command or not all(
        isinstance(argument, str) and argument for argument in command
    ):
        raise ConfigurationError(
            f"{name!r} must contain at least one non-empty argument"
        )

    return tuple(command)


def load_agent_commands(folder: Path) -> AgentCommands:
    path = folder / CONFIG_FILENAME
    if not path.is_file():
        raise ConfigurationError(
            f"{CONFIG_FILENAME} is missing from {folder}"
        )

    try:
        contents = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"could not read {CONFIG_FILENAME}: {error}"
        ) from error

    if not isinstance(contents, Mapping):
        raise ConfigurationError(
            f"{CONFIG_FILENAME} must contain a JSON object"
        )

    missing = [
        name for name in ("researcher", "supervisor") if name not in contents
    ]
    if missing:
        raise ConfigurationError(
            f"{CONFIG_FILENAME} is missing: {', '.join(missing)}"
        )

    return AgentCommands(
        researcher=_parse_command("researcher", contents["researcher"]),
        supervisor=_parse_command("supervisor", contents["supervisor"]),
    )


def ensure_agent_config(folder: Path) -> tuple[Path, bool]:
    path = folder / CONFIG_FILENAME
    if path.exists():
        return path, False

    try:
        path.write_text(default_config_text(), encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(
            f"could not create {path}: {error}"
        ) from error

    return path, True


def setup_message(folder: Path, error: ConfigurationError) -> str:
    return (
        f"agent-run: {error}\n\n"
        f"Fix {folder / CONFIG_FILENAME}, then rerun agent-run. "
        f"Expected format:\n\n{default_config_text()}"
    )
