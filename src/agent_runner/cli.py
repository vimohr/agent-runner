"""Command-line launcher for running the orchestrator in a project folder."""

import argparse
import math
from pathlib import Path
import subprocess
import sys
from typing import Optional, Sequence

from . import orchestrator
from .config import (
    ConfigurationError,
    ensure_agent_config,
    load_agent_commands,
    setup_message,
)
from .documents import find_existing_pdf


BRANCH_NAME = "agent"


def positive_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return number


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


def is_git_repository(folder: Path) -> bool:
    result = git("rev-parse", "--is-inside-work-tree", cwd=folder, check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def switch_to_agent_branch(folder: Path) -> None:
    if not is_git_repository(folder):
        git("init", "-b", BRANCH_NAME, cwd=folder)
        return

    current = git("branch", "--show-current", cwd=folder).stdout.strip()
    if current == BRANCH_NAME:
        return

    branch_exists = git(
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{BRANCH_NAME}",
        cwd=folder,
        check=False,
    ).returncode == 0

    if branch_exists:
        git("switch", BRANCH_NAME, cwd=folder)
    else:
        git("switch", "-c", BRANCH_NAME, cwd=folder)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-run",
        description="Run the agent orchestrator for a marked project folder.",
    )
    parser.add_argument("folder", type=Path)
    parser.add_argument(
        "--timeout",
        type=positive_number,
        metavar="SECONDS",
        help="stop an individual researcher or supervisor after this long",
    )
    parser.add_argument(
        "--heartbeat",
        type=positive_number,
        default=30.0,
        metavar="SECONDS",
        help="interval for running-agent status messages (default: 30)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {orchestrator.__version__}",
    )
    return parser


def confirm_default_config(config_path: Path) -> bool:
    question = (
        f"You should review and modify {config_path}. "
        "Continue with the standard settings? [y/N]: "
    )
    while True:
        try:
            answer = input(question).strip().lower()
        except EOFError:
            return False

        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        print("Please answer yes or no.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        parser.error(f"not a directory: {folder}")

    if not (folder / "TASK.md").is_file() and find_existing_pdf(folder) is None:
        return 0

    try:
        config_path, created = ensure_agent_config(folder)
        if created:
            print(f"Created {config_path} with the standard agent settings.")
            if not confirm_default_config(config_path):
                print(f"Stopped. Edit {config_path}, then rerun agent-run.")
                return 0
        else:
            print(f"Using agent configuration from {config_path}.")
        commands = load_agent_commands(folder)
        switch_to_agent_branch(folder)
        orchestrator.main(
            folder,
            commands,
            timeout_seconds=args.timeout,
            heartbeat_seconds=args.heartbeat,
        )
    except ConfigurationError as error:
        print(setup_message(folder, error), file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        parser.error(f"required command not found: {error.filename}")
    except subprocess.CalledProcessError as error:
        if error.stdout:
            print(error.stdout, end="")
        if error.stderr:
            print(error.stderr, end="", file=sys.stderr)
        return error.returncode
    except RuntimeError as error:
        print(f"agent-run: {error}", file=sys.stderr)
        return 1

    return 0
