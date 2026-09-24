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
from .config import AgentCommands, prompts_for
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
                b".agent-run", b"feedback.md", b"agent-run.json",
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


def validate_feedback() -> str:
    if not FEEDBACK.exists():
        raise RuntimeError("Supervisor did not create feedback.md")

    text = FEEDBACK.read_text()
    if "STATUS:" not in text:
        raise RuntimeError("feedback.md is missing STATUS")

    return text


def render_prompt(template: str, **values: object) -> str:
    """Replace the documented, deliberately explicit prompt placeholders."""
    prompt = template
    for name, value in values.items():
        prompt = prompt.replace(f"{{{{{name}}}}}", str(value))
    return prompt.strip() + "\n"


def run_researcher(iteration: int) -> str:
    feedback_instruction = ""
    task_instruction = ""

    if (ROOT / "TASK.md").exists():
        task_instruction = "Read TASK.md and use it as the project brief."

    if FEEDBACK.exists():
        feedback_instruction = """Read feedback.md carefully.

Address every CRITICAL and MAJOR issue.
Address MINOR issues when appropriate."""

    pdf_path = pdf_project_path()

    if COMMANDS is None:
        raise RuntimeError("Agent commands have not been configured")

    researcher_prompt, _ = prompts_for(COMMANDS)
    prompt = render_prompt(
        researcher_prompt,
        iteration=iteration,
        pdf_path=pdf_path,
        task_instruction=task_instruction,
        feedback_instruction=feedback_instruction,
    )

    return run(
        [*COMMANDS.researcher, prompt],
        label=f"researcher iteration {iteration}",
    )


def run_supervisor(iteration: int) -> str:
    pdf_path = pdf_project_path()

    if COMMANDS is None:
        raise RuntimeError("Agent commands have not been configured")

    _, supervisor_prompt = prompts_for(COMMANDS)
    prompt = render_prompt(
        supervisor_prompt,
        iteration=iteration,
        pdf_path=pdf_path,
    )

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
        commit_researcher_changes(iteration)

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
