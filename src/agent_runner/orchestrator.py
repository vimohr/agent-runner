"""Researcher and supervisor agent orchestration loop."""

from pathlib import Path
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
from typing import Optional, Sequence, TextIO

from . import __version__
from .config import AgentCommands
from .documents import find_existing_pdf


ROOT = Path.cwd()
PAPER = ROOT / "paper.pdf"
FEEDBACK = ROOT / "feedback.md"

# Allow some back-and-forth while still bounding a runaway review cycle.
MAX_ITERATIONS = 100

COMMANDS: Optional[AgentCommands] = None
AGENT_TIMEOUT_SECONDS: Optional[float] = None
HEARTBEAT_SECONDS = 30.0


def _elapsed(seconds: float) -> str:
    minutes, remainder = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {remainder}s"
    if minutes:
        return f"{minutes}m {remainder}s"
    return f"{remainder}s"


def _stop_process(process: subprocess.Popen[str]) -> None:
    """Stop the agent and any subprocesses it started."""
    if process.poll() is not None:
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (AttributeError, ProcessLookupError, PermissionError):
        process.terminate()

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError, PermissionError):
            process.kill()


def select_pdf(project_root: Path) -> Path:
    # TASK.md-only projects continue to create paper.pdf at the root.
    return find_existing_pdf(project_root) or project_root / "paper.pdf"


def pdf_project_path() -> str:
    return PAPER.relative_to(ROOT).as_posix()


def run(
    cmd: Sequence[str],
    cwd: Optional[Path] = None,
    *,
    label: str = "agent",
) -> str:
    """Run an agent while teeing its output to the terminal and a log file."""
    working_directory = cwd or ROOT
    log_directory = ROOT / ".agent-run"
    log_directory.mkdir(exist_ok=True)
    log_path = log_directory / "agent-run.log"
    started = time.monotonic()
    display_command = shlex.join(cmd[:-1]) + " <prompt>"

    process = subprocess.Popen(
        cmd,
        cwd=working_directory,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        errors="replace",
        start_new_session=True,
    )

    print(
        f"[{label}] started PID {process.pid}; live output follows. "
        f"Log: {log_path}"
    )

    output: queue.Queue[tuple[str, Optional[str]]] = queue.Queue()

    def read_stream(name: str, stream: TextIO) -> None:
        try:
            for line in stream:
                output.put((name, line))
        finally:
            output.put((name, None))

    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
        threading.Thread(
            target=read_stream,
            args=("stdout", process.stdout),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=("stderr", process.stderr),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    stdout: list[str] = []
    completed_streams = 0
    next_heartbeat = started + HEARTBEAT_SECONDS
    timed_out = False

    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S %z")
        log.write(
            f"\n=== {timestamp} | {label} | PID {process.pid} ===\n"
            f"cwd: {working_directory}\ncommand: {display_command}\n"
        )

        try:
            while completed_streams < len(readers):
                now = time.monotonic()
                if (
                    AGENT_TIMEOUT_SECONDS is not None
                    and now - started >= AGENT_TIMEOUT_SECONDS
                    and not timed_out
                    and process.poll() is None
                ):
                    timed_out = True
                    message = (
                        f"[{label}] timed out after "
                        f"{_elapsed(now - started)}; stopping PID {process.pid}."
                    )
                    print(message, file=sys.stderr, flush=True)
                    log.write(message + "\n")
                    _stop_process(process)

                if (
                    now >= next_heartbeat
                    and not timed_out
                    and process.poll() is None
                ):
                    message = (
                        f"[{label}] still running (PID {process.pid}, "
                        f"elapsed {_elapsed(now - started)})."
                    )
                    print(message, file=sys.stderr, flush=True)
                    log.write(message + "\n")
                    next_heartbeat = now + HEARTBEAT_SECONDS

                try:
                    stream_name, line = output.get(timeout=0.25)
                except queue.Empty:
                    continue

                if line is None:
                    completed_streams += 1
                    continue

                destination = sys.stdout if stream_name == "stdout" else sys.stderr
                print(line, end="", file=destination, flush=True)
                log.write(f"[{stream_name}] {line}")
                if stream_name == "stdout":
                    stdout.append(line)
        except KeyboardInterrupt:
            message = f"[{label}] interrupted; stopping PID {process.pid}."
            print(message, file=sys.stderr, flush=True)
            log.write(message + "\n")
            _stop_process(process)
            raise

        returncode = process.wait()
        elapsed = _elapsed(time.monotonic() - started)
        log.write(f"=== exit {returncode} | elapsed {elapsed} ===\n")

    if timed_out:
        raise RuntimeError(
            f"{label} exceeded the {AGENT_TIMEOUT_SECONDS:g}s timeout. "
            f"See {log_path}."
        )

    if returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code {returncode}. See {log_path}."
        )

    print(f"[{label}] finished successfully in {elapsed}.")
    return "".join(stdout)


def validate_pdf() -> None:
    pdf_path = pdf_project_path()
    if not PAPER.exists():
        raise RuntimeError(f"Researcher did not create {pdf_path}")

    if PAPER.stat().st_size < 1000:
        raise RuntimeError(f"{pdf_path} appears invalid or empty")


def validate_feedback() -> str:
    if not FEEDBACK.exists():
        raise RuntimeError("Supervisor did not create feedback.md")

    text = FEEDBACK.read_text()
    if "STATUS:" not in text:
        raise RuntimeError("feedback.md is missing STATUS")

    return text


def run_researcher(iteration: int) -> str:
    feedback_instruction = ""
    task_instruction = ""

    if (ROOT / "TASK.md").exists():
        task_instruction = """
Read TASK.md and use it as the project brief.
"""

    if FEEDBACK.exists():
        feedback_instruction = """
Read feedback.md carefully.

Address every CRITICAL and MAJOR issue.
Address MINOR issues when appropriate.
"""

    pdf_path = pdf_project_path()

    prompt = f"""
You are the RESEARCHER / AUTHOR.

This is iteration {iteration}.

Work inside the current project directory.

Your responsibility is to research, write, and revise the academic paper.

{task_instruction}

{feedback_instruction}

Do a thorough literature analysis for context when appropriate.

Requirements:

1. Maintain the editable source of the paper.
2. Incorporate supervisor feedback where supplied and when you consider it appropriate.
3. Compile/export the completed paper to:

   {pdf_path}

4. Verify that {pdf_path} was successfully generated.
5. Do NOT create or modify feedback.md.
6. Do NOT decide whether the project is finished.

Finish only when {pdf_path} represents your completed work for this iteration.
"""

    if COMMANDS is None:
        raise RuntimeError("Agent commands have not been configured")

    return run(
        [*COMMANDS.researcher, prompt],
        label=f"researcher iteration {iteration}",
    )


def run_supervisor(iteration: int) -> str:
    pdf_path = pdf_project_path()
    prompt = f"""
You are the SUPERVISOR / REVIEWER.

This is review iteration {iteration}.

Read:

{pdf_path}

Do a thorough literature review and critically evaluate the paper. What should be done to make this really a good paper?

DO NOT edit the paper.
DO NOT rewrite the paper source.
DO NOT modify {pdf_path}.

Create exactly:

feedback.md

Use this structure:

# Supervisor Review

Iteration: {iteration}

## Critical Issues

## Major Issues

## Minor Issues

## Overall Assessment

## Decision

End with exactly one of:

STATUS: REVISE

or

STATUS: READY

For every issue explain:

- where it occurs
- what the problem is
- why it matters
- what the researcher should change

Use STATUS: READY only if there are no remaining issues that materially
prevent the paper from being submission-ready.
"""

    if COMMANDS is None:
        raise RuntimeError("Agent commands have not been configured")

    return run(
        [*COMMANDS.supervisor, prompt],
        label=f"supervisor iteration {iteration}",
    )


def main(
    project_root: Path,
    commands: AgentCommands,
    *,
    timeout_seconds: Optional[float] = None,
    heartbeat_seconds: float = 30.0,
) -> None:
    global ROOT, PAPER, FEEDBACK, COMMANDS
    global AGENT_TIMEOUT_SECONDS, HEARTBEAT_SECONDS

    ROOT = Path(project_root).resolve()
    PAPER = select_pdf(ROOT)
    FEEDBACK = ROOT / "feedback.md"
    COMMANDS = commands
    AGENT_TIMEOUT_SECONDS = timeout_seconds
    HEARTBEAT_SECONDS = heartbeat_seconds

    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n=== ITERATION {iteration} ===")

        print("Running researcher...")
        run_researcher(iteration)

        validate_pdf()
        print(f"{pdf_project_path()} validated.")

        # A stale file must not count as the new supervisor review.
        if FEEDBACK.exists():
            FEEDBACK.unlink()

        print("Running supervisor...")
        run_supervisor(iteration)

        feedback = validate_feedback()
        print("feedback.md validated.")

        if "STATUS: READY" in feedback:
            print("\nSupervisor marked paper READY.")
            return

        if "STATUS: REVISE" not in feedback:
            raise RuntimeError(
                "Supervisor returned neither READY nor REVISE."
            )

        print("Supervisor requested another revision.")

    raise RuntimeError(
        f"Maximum iterations ({MAX_ITERATIONS}) reached."
    )
