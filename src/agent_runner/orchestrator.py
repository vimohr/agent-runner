"""Researcher, supervisor, and independent referee orchestration loop."""

from pathlib import Path
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from typing import Optional, Sequence, TextIO

from . import __version__
from .config import AgentCommands, prompts_for, reviewer_command_for
from .documents import find_existing_pdf


ROOT = Path.cwd()
PAPER = ROOT / "paper.pdf"
FEEDBACK = ROOT / "feedback.md"
REVIEWER_FEEDBACK = ROOT / "reviewer-feedback.md"

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


def commit_researcher_changes(iteration: int) -> None:
    """Commit project changes while keeping runner and reviewer files out."""
    def researcher_paths(*git_args: str) -> list[str]:
        result = subprocess.run(
            ["git", *git_args], cwd=ROOT, check=True, capture_output=True,
        )
        return [
            ":(literal)" + os.fsdecode(path)
            for path in result.stdout.split(b"\0")
            if path and path.split(b"/", 1)[0] not in {
                b".agent-run", b"feedback.md", b"reviewer-feedback.md",
                b"agent-run.json",
            }
        ]

    # Git reports explicitly excluded ignored paths as an error to `git add`.
    # Enumerate tracked and non-ignored untracked files instead.
    paths = researcher_paths(
        "ls-files", "-z", "--cached", "--others", "--exclude-standard",
    )
    if paths:
        subprocess.run(["git", "add", "-A", "--", *paths], cwd=ROOT, check=True)
    staged_paths = researcher_paths("diff", "--cached", "--name-only", "-z")
    if not staged_paths:
        print("No researcher changes to commit.")
        return
    changes = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", *staged_paths],
        cwd=ROOT,
        check=False,
    )
    if changes.returncode == 0:
        print("No researcher changes to commit.")
        return
    if changes.returncode != 1:
        raise RuntimeError("Could not inspect staged researcher changes")

    subprocess.run(
        [
            "git", "commit", "--only",
            "-m", f"Researcher iteration {iteration}: update paper",
            "--", *staged_paths,
        ],
        cwd=ROOT,
        check=True,
    )


def validate_decision(path: Path, role: str, statuses: set[str]) -> str:
    if not path.is_file():
        raise RuntimeError(f"{role} did not create {path.name}")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        raise RuntimeError(f"{path.name} is empty")
    match = re.fullmatch(r"STATUS: ([A-Z]+)", lines[-1].strip())
    if match is None or match.group(1) not in statuses:
        raise RuntimeError(
            f"{path.name} must end with exactly one of: "
            + ", ".join(f"STATUS: {status}" for status in sorted(statuses))
        )
    if sum(line.strip().startswith("STATUS:") for line in lines) != 1:
        raise RuntimeError(f"{path.name} must contain exactly one STATUS line")
    return match.group(1)


def validate_feedback() -> str:
    return validate_decision(FEEDBACK, "Supervisor", {"READY", "REVISE"})


def validate_reviewer_feedback() -> str:
    return validate_decision(
        REVIEWER_FEEDBACK, "Reviewer", {"ACCEPT", "REVISE", "REJECT"}
    )


def render_prompt(template: str, **values: object) -> str:
    """Replace the documented, deliberately explicit prompt placeholders."""
    prompt = template
    for name, value in values.items():
        prompt = prompt.replace(f"{{{{{name}}}}}", str(value))
    return prompt.strip() + "\n"


def run_researcher(iteration: int) -> str:
    feedback_instruction = (
        "Read feedback.md and reviewer-feedback.md when present. "
        "Supervisor feedback takes priority if the two conflict."
    )
    task_instruction = ""

    if (ROOT / "TASK.md").exists():
        task_instruction = "Read TASK.md and use it as the project brief."

    if FEEDBACK.exists():
        feedback_instruction += """

Read feedback.md carefully.

Address every CRITICAL and MAJOR issue.
Address MINOR issues when appropriate."""

    if REVIEWER_FEEDBACK.exists():
        feedback_instruction += """

Read reviewer-feedback.md carefully. Address the referee's rejection or
revision requests, including evidence, analysis, and manuscript changes."""

    pdf_path = pdf_project_path()

    if COMMANDS is None:
        raise RuntimeError("Agent commands have not been configured")

    researcher_prompt, _, _ = prompts_for(COMMANDS)
    prompt = render_prompt(
        researcher_prompt,
        iteration=iteration,
        pdf_path=pdf_path,
        task_instruction=task_instruction,
        feedback_instruction=feedback_instruction,
    )
    if "{{feedback_instruction}}" not in researcher_prompt:
        prompt += feedback_instruction + "\n"

    return run(
        [*COMMANDS.researcher, prompt],
        label=f"researcher iteration {iteration}",
    )


def run_supervisor(iteration: int) -> str:
    pdf_path = pdf_project_path()

    if COMMANDS is None:
        raise RuntimeError("Agent commands have not been configured")

    _, supervisor_prompt, _ = prompts_for(COMMANDS)
    reviewer_instruction = (
        "Read reviewer-feedback.md as historical context: it explains why an "
        "earlier draft was rejected or sent for revision. Independently assess "
        "the current draft and verify that its substantive concerns are resolved "
        "before marking the paper READY."
        if REVIEWER_FEEDBACK.exists() else ""
    )
    prompt = render_prompt(
        supervisor_prompt,
        iteration=iteration,
        pdf_path=pdf_path,
        reviewer_feedback_instruction=reviewer_instruction,
    )
    if (
        reviewer_instruction
        and "{{reviewer_feedback_instruction}}" not in supervisor_prompt
    ):
        prompt += reviewer_instruction + "\n"
    return run(
        [*COMMANDS.supervisor, prompt],
        label=f"supervisor iteration {iteration}",
    )


def run_reviewer(iteration: int) -> str:
    if COMMANDS is None:
        raise RuntimeError("Agent commands have not been configured")
    _, _, reviewer_prompt = prompts_for(COMMANDS)
    prompt = render_prompt(
        reviewer_prompt, iteration=iteration, pdf_path=pdf_project_path(),
    )
    return run(
        [*reviewer_command_for(COMMANDS), prompt],
        label=f"reviewer round {iteration}",
    )


def main(
    project_root: Path,
    commands: AgentCommands,
    *,
    timeout_seconds: Optional[float] = None,
    heartbeat_seconds: float = 30.0,
) -> None:
    global ROOT, PAPER, FEEDBACK, REVIEWER_FEEDBACK, COMMANDS
    global AGENT_TIMEOUT_SECONDS, HEARTBEAT_SECONDS

    ROOT = Path(project_root).resolve()
    PAPER = select_pdf(ROOT)
    FEEDBACK = ROOT / "feedback.md"
    REVIEWER_FEEDBACK = ROOT / "reviewer-feedback.md"
    COMMANDS = commands
    AGENT_TIMEOUT_SECONDS = timeout_seconds
    HEARTBEAT_SECONDS = heartbeat_seconds

    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n=== ITERATION {iteration} ===")

        print("Running researcher...")
        run_researcher(iteration)

        # The next supervisor must not see its own previous report.
        FEEDBACK.unlink(missing_ok=True)

        validate_pdf()
        print(f"{pdf_project_path()} validated.")
        commit_researcher_changes(iteration)

        print("Running supervisor...")
        run_supervisor(iteration)

        feedback = validate_feedback()
        print("feedback.md validated.")

        if feedback == "REVISE":
            print("Supervisor requested another revision.")
            continue

        print("Supervisor marked paper READY. Running independent reviewer...")
        # The external reviewer judges this draft without previous reports.
        FEEDBACK.unlink(missing_ok=True)
        REVIEWER_FEEDBACK.unlink(missing_ok=True)
        run_reviewer(iteration)
        decision = validate_reviewer_feedback()
        print("reviewer-feedback.md validated.")
        if decision == "ACCEPT":
            print("\nReviewer accepted the paper.")
            return
        print(f"Reviewer requested another revision ({decision}).")

    raise RuntimeError(
        f"Maximum iterations ({MAX_ITERATIONS}) reached."
    )
