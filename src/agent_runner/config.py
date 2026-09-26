"""Per-project agent command configuration."""

from dataclasses import dataclass
from importlib import resources
import json
from pathlib import Path
import shlex
from typing import Mapping, Optional, Sequence


CONFIG_FILENAME = "agent-run.json"
DEFAULT_CONFIG_FILENAME = "default-agent-run.json"
DEFAULT_JOURNAL = "Physical Review style journal"
LEGACY_RESEARCHER_COMMIT_INSTRUCTION = (
    "7. When your work for this iteration is complete, commit all "
    "paper-related changes to Git with a descriptive commit message. "
    "Do not commit .agent-run logs or unrelated files."
)


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
    researcher_prompt: Optional[str] = None
    supervisor_prompt: Optional[str] = None
    reviewer: Optional[tuple[str, ...]] = None
    reviewer_prompt: Optional[str] = None
    journal: str = DEFAULT_JOURNAL


def _parse_journal(value: object) -> str:
    if value is None:
        return DEFAULT_JOURNAL
    if not isinstance(value, str):
        raise ConfigurationError("'journal' must be a string")
    journal = value.strip()
    if any(ord(character) < 32 for character in journal):
        raise ConfigurationError("'journal' must be a single line")
    return journal or DEFAULT_JOURNAL


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


def _parse_prompt(name: str, value: object) -> str:
    if isinstance(value, str):
        prompt = value
    elif isinstance(value, list) and all(
        isinstance(line, str) for line in value
    ):
        prompt = "\n".join(value)
    else:
        raise ConfigurationError(
            f"{name!r} must be a string or an array of strings"
        )

    if not prompt.strip():
        raise ConfigurationError(f"{name!r} must not be empty")
    return prompt


def _defaults() -> Mapping[str, object]:
    return json.loads(default_config_text())


def _default_prompts() -> tuple[str, str, str]:
    contents = _defaults()
    return (
        _parse_prompt("researcher_prompt", contents["researcher_prompt"]),
        _parse_prompt("supervisor_prompt", contents["supervisor_prompt"]),
        _parse_prompt("reviewer_prompt", contents["reviewer_prompt"]),
    )


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

    (default_researcher_prompt, default_supervisor_prompt,
     default_reviewer_prompt) = _default_prompts()
    return AgentCommands(
        researcher=_parse_command("researcher", contents["researcher"]),
        supervisor=_parse_command("supervisor", contents["supervisor"]),
        researcher_prompt=_parse_prompt(
            "researcher_prompt",
            contents.get("researcher_prompt", default_researcher_prompt),
        ).replace(LEGACY_RESEARCHER_COMMIT_INSTRUCTION, ""),
        supervisor_prompt=_parse_prompt(
            "supervisor_prompt",
            contents.get("supervisor_prompt", default_supervisor_prompt),
        ),
        reviewer=_parse_command(
            "reviewer", contents.get("reviewer", _defaults()["reviewer"]),
        ),
        reviewer_prompt=_parse_prompt(
            "reviewer_prompt",
            contents.get("reviewer_prompt", default_reviewer_prompt),
        ),
        journal=_parse_journal(contents.get("journal")),
    )


def prompts_for(commands: AgentCommands) -> tuple[str, str, str]:
    """Return configured prompts, falling back for programmatic callers."""
    if (
        commands.researcher_prompt is not None
        and commands.supervisor_prompt is not None
        and commands.reviewer_prompt is not None
    ):
        return (commands.researcher_prompt, commands.supervisor_prompt,
                commands.reviewer_prompt)

    default_researcher, default_supervisor, default_reviewer = _default_prompts()
    return (
        commands.researcher_prompt or default_researcher,
        commands.supervisor_prompt or default_supervisor,
        commands.reviewer_prompt or default_reviewer,
    )


def reviewer_command_for(commands: AgentCommands) -> tuple[str, ...]:
    return commands.reviewer or _parse_command("reviewer", _defaults()["reviewer"])


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
