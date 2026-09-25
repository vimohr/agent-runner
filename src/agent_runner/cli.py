"""Command-line launcher for running the orchestrator in a project folder."""

import argparse
from email.message import EmailMessage
from email.utils import parseaddr
import math
import os
from pathlib import Path
import shutil
import smtplib
import socket
import ssl
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


def email_address(value: str) -> str:
    _, address = parseaddr(value)
    if (
        address != value or address.count("@") != 1
        or any(character.isspace() or ord(character) < 32 for character in value)
        or not all(address.split("@"))
    ):
        raise argparse.ArgumentTypeError("must be a single email address")
    return value


def smtp_settings() -> Optional[tuple[str, int, str, str, Optional[str], Optional[str]]]:
    host = os.environ.get("AGENT_RUN_SMTP_HOST")
    if not host:
        return None

    security = os.environ.get("AGENT_RUN_SMTP_SECURITY", "starttls").lower()
    if security not in {"starttls", "ssl", "none"}:
        raise RuntimeError("AGENT_RUN_SMTP_SECURITY must be starttls, ssl, or none")
    default_port = {"starttls": 587, "ssl": 465, "none": 25}[security]
    try:
        port = int(os.environ.get("AGENT_RUN_SMTP_PORT", str(default_port)))
    except ValueError as error:
        raise RuntimeError("AGENT_RUN_SMTP_PORT must be a valid port") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("AGENT_RUN_SMTP_PORT must be a valid port")

    sender = os.environ.get("AGENT_RUN_EMAIL_FROM", "")
    try:
        email_address(sender)
    except argparse.ArgumentTypeError as error:
        raise RuntimeError("AGENT_RUN_EMAIL_FROM must be a single email address") from error
    username = os.environ.get("AGENT_RUN_SMTP_USERNAME")
    password = os.environ.get("AGENT_RUN_SMTP_PASSWORD")
    if bool(username) != bool(password):
        raise RuntimeError("SMTP username and password must both be set")
    if security == "none" and username:
        raise RuntimeError("SMTP authentication requires TLS")
    return host, port, security, sender, username, password


def local_mail_transport() -> tuple[str, str]:
    sendmail = shutil.which("sendmail")
    if sendmail:
        return "sendmail", sendmail
    for command in ("mail", "mailx"):
        path = shutil.which(command)
        if path:
            return command, path
    standard_path = Path("/usr/sbin/sendmail")
    if standard_path.is_file():
        return "sendmail", str(standard_path)
    raise RuntimeError(
        "cannot send completion email: configure AGENT_RUN_SMTP_HOST "
        "or install sendmail, mail, or mailx"
    )


def check_email_delivery() -> None:
    if smtp_settings() is None:
        local_mail_transport()


def send_completion_email(recipient: str, folder: Path) -> None:
    settings = smtp_settings()

    message = EmailMessage()
    message["From"] = settings[3] if settings else f"agent-run@{socket.getfqdn()}"
    message["To"] = recipient
    message["Subject"] = f"agent-run completed: {folder.name}"
    message.set_content(
        "The supervisor marked the paper READY and the external reviewer accepted it.\n\n"
        f"Project: {folder}\n"
        f"PDF: {orchestrator.select_pdf(folder)}\n"
    )
    if settings:
        host, port, security, _, username, password = settings
        try:
            smtp_class = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
            with smtp_class(host, port, timeout=30) as smtp:
                if security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            raise RuntimeError(f"completion email failed: {error}") from error
        return

    transport, command = local_mail_transport()
    if transport == "sendmail":
        arguments = [command, "-t", "-i"]
        content = message.as_bytes()
    else:
        arguments = [command, "-s", message["Subject"], recipient]
        content = message.get_content().encode()
    try:
        result = subprocess.run(
            arguments, input=content,
            capture_output=True, check=False, timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("completion email timed out after 30 seconds") from error
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"completion email failed ({transport} exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )


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
        "--email", type=email_address, metavar="ADDRESS",
        help="email ADDRESS when the reviewer accepts the paper",
    )
    parser.add_argument(
        "--timeout",
        type=positive_number,
        metavar="SECONDS",
        help="stop an individual researcher, supervisor, or reviewer after this long",
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
        if args.email:
            check_email_delivery()
        switch_to_agent_branch(folder)
        orchestrator.main(
            folder,
            commands,
            timeout_seconds=args.timeout,
            heartbeat_seconds=args.heartbeat,
        )
        if args.email:
            send_completion_email(args.email, folder)
            print(f"Completion email sent to {args.email}.")
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
