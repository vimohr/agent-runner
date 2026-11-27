"""Researcher, supervisor, and independent referee orchestration loop."""

from collections import deque
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
from .checkpoint import load_checkpoint, project_checkpoint, save_checkpoint
from .config import (
    AgentCommands, DEFAULT_JOURNAL, prompt_for, reviewer_command_for,
)
from .documents import find_existing_pdf, find_task
from .dashboard import GIST_TOKEN_ENV, RunDashboard


ROOT = Path.cwd()
PAPER = ROOT / "paper.pdf"
FEEDBACK = ROOT / "feedback.md"
REVIEWER_FEEDBACK = ROOT / "reviewer-feedback.md"

# Allow some back-and-forth while still bounding a runaway review cycle.
MAX_ITERATIONS = 100

COMMANDS: Optional[AgentCommands] = None
AGENT_TIMEOUT_SECONDS: Optional[float] = None
HEARTBEAT_SECONDS = 30.0
STREAM_AGENT_OUTPUT = False
DASHBOARD: Optional[RunDashboard] = None
CAPACITY_RETRY_DELAYS = (15, 30, 60)

LITERATURE_SEARCH_INSTRUCTION = (
    "On every turn, conduct your own thorough literature search before choosing work, "
    "drafting or revising the manuscript, or making a review decision. Search scholarly "
    "indexes or repositories relevant to the field using the question, method, and "
    "alternative terminology. Follow citations to foundational work and check recent "
    "publications and relevant preprints for the current state of the art.\n"
    "Read the closest primary sources and compare their methods, assumptions, and "
    "results with the project's proposed contribution. Cite the sources you verified "
    "using a DOI, stable URL, or other traceable reference. In your assigned output, "
    "explain what was already established and what contribution, if any, remains. "
    "If access or coverage is limited, record what you could search and qualify any "
    "novelty claims accordingly. An unsuccessful search alone cannot establish novelty."
)


class ModelCapacityError(RuntimeError):
    """An agent exited because its selected model was temporarily at capacity."""


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
    terminal_label: Optional[str] = None,
) -> str:
    """Run an agent, logging all output and optionally streaming it live."""
    working_directory = cwd or ROOT
    log_directory = ROOT / ".agent-run"
    log_directory.mkdir(exist_ok=True)
    log_path = log_directory / "agent-run.log"
    started = time.monotonic()
    terminal_label = terminal_label or label
    display_command = shlex.join(cmd[:-1]) + " <prompt>"

    process = subprocess.Popen(
        cmd,
        cwd=working_directory,
        env={key: value for key, value in os.environ.items() if key != GIST_TOKEN_ENV},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        errors="replace",
        start_new_session=True,
    )

    print(
        f"[{terminal_label}] started PID {process.pid}. "
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
    recent_stdout: deque[str] = deque(maxlen=6)
    recent_stderr: deque[str] = deque(maxlen=6)
    capacity_error = False
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
                    detail = (
                        f"timed out after {_elapsed(now - started)}; "
                        f"stopping PID {process.pid}."
                    )
                    print(f"[{terminal_label}] {detail}", file=sys.stderr, flush=True)
                    log.write(f"[{label}] {detail}\n")
                    _stop_process(process)

                if (
                    now >= next_heartbeat
                    and not timed_out
                    and process.poll() is None
                ):
                    detail = (
                        f"still running (PID {process.pid}, "
                        f"elapsed {_elapsed(now - started)})."
                    )
                    print(f"[{terminal_label}] {detail}", file=sys.stderr, flush=True)
                    log.write(f"[{label}] {detail}\n")
                    next_heartbeat = now + HEARTBEAT_SECONDS
                    if DASHBOARD is not None:
                        DASHBOARD.heartbeat()

                try:
                    stream_name, line = output.get(timeout=0.25)
                except queue.Empty:
                    continue

                if line is None:
                    completed_streams += 1
                    continue

                if STREAM_AGENT_OUTPUT:
                    destination = sys.stdout if stream_name == "stdout" else sys.stderr
                    print(line, end="", file=destination, flush=True)
                log.write(f"[{stream_name}] {line}")
                stripped = line.strip()
                if stripped:
                    recent = recent_stdout if stream_name == "stdout" else recent_stderr
                    recent.append(stripped[-300:])
                if "model is at capacity" in line.lower():
                    capacity_error = True
                if stream_name == "stdout":
                    stdout.append(line)
        except KeyboardInterrupt:
            detail = f"interrupted; stopping PID {process.pid}."
            print(f"[{terminal_label}] {detail}", file=sys.stderr, flush=True)
            log.write(f"[{label}] {detail}\n")
            _stop_process(process)
            raise

        returncode = process.wait()
        elapsed = _elapsed(time.monotonic() - started)
        log.write(f"=== exit {returncode} | elapsed {elapsed} ===\n")

    if (timed_out or returncode != 0) and not STREAM_AGENT_OUTPUT:
        recent = recent_stderr or recent_stdout
        if recent:
            print(f"[{terminal_label}] recent agent output:", file=sys.stderr)
            for line in recent:
                print(f"  {line}", file=sys.stderr)

    if timed_out:
        raise RuntimeError(
            f"{label} exceeded the {AGENT_TIMEOUT_SECONDS:g}s timeout. "
            f"See {log_path}."
        )

    if returncode != 0:
        if capacity_error:
            raise ModelCapacityError(
                f"{label} model was at capacity (exit {returncode}). "
                f"See {log_path}."
            )
        raise RuntimeError(
            f"{label} failed with exit code {returncode}. See {log_path}."
        )

    print(f"[{terminal_label}] finished successfully in {elapsed}.")
    return "".join(stdout)


def run_with_capacity_retry(
    cmd: Sequence[str], *, label: str, terminal_label: str,
    result_path: Optional[Path] = None,
) -> str:
    attempts = len(CAPACITY_RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            return run(cmd, label=label, terminal_label=terminal_label)
        except ModelCapacityError as error:
            if attempt == attempts:
                raise RuntimeError(
                    f"{label} model remained at capacity after {attempts} "
                    f"attempts. See {ROOT / '.agent-run' / 'agent-run.log'}."
                ) from error
            delay = CAPACITY_RETRY_DELAYS[attempt - 1]
            if result_path is not None:
                result_path.unlink(missing_ok=True)
            print(
                f"[{terminal_label}] model at capacity; retrying in {delay}s "
                f"(attempt {attempt + 1}/{attempts}).",
                file=sys.stderr, flush=True,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def validate_pdf() -> None:
    pdf_path = pdf_project_path()
    if not PAPER.exists():
        raise RuntimeError(f"Researcher did not create {pdf_path}")

    if PAPER.stat().st_size < 1000:
        raise RuntimeError(f"{pdf_path} appears invalid or empty")


def commit_researcher_changes(iteration: int, *, investigating: bool = False) -> None:
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

    subject = "update investigation" if investigating else "update paper"
    subprocess.run(
        [
            "git", "commit", "--only",
            "-m", f"Researcher: {subject}",
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


def validate_reviewer_feedback() -> str:
    return validate_decision(
        REVIEWER_FEEDBACK, "Reviewer", {"ACCEPT", "REVISE", "REJECT"}
    )


def render_prompt(template: str, **values: object) -> str:
    """Replace the documented, deliberately explicit prompt placeholders."""
    # Ignore superseded lines from older generated project configs.
    prompt = "\n".join(
        line for line in template.splitlines()
        if "{{iteration}}" not in line
        and line.strip() != "Do a thorough literature analysis for context when appropriate."
    )
    for name, value in values.items():
        prompt = prompt.replace(f"{{{{{name}}}}}", str(value))
    return LITERATURE_SEARCH_INSTRUCTION + "\n\n" + prompt.strip() + "\n"


def journal_instruction(journal: str, *, paper_mode: bool = False) -> str:
    if journal == DEFAULT_JOURNAL:
        task = find_task(ROOT)
        if task and not paper_mode:
            return (
                f"Use the project brief in {task.name} to determine the paper's "
                "audience and disciplinary conventions. If it names a target journal, "
                "consult its current official guidance.\n\n"
            )
        journal = "Physical Review style journal"
    if journal == "Physical Review style journal":
        return (
            f"IMPORTANT — Target style: {journal}. Before starting, consult "
            "the current official Physical Review family author guidance, "
            "scope, paper style, and editorial criteria. Apply that general "
            "style without assuming rules for a specific journal title.\n\n"
        )
    return (
        f"IMPORTANT — Target journal: {journal}. Before starting, consult "
        "the current official scope, author instructions, article format, "
        "paper style, and editorial or review criteria for this exact journal. "
        "Apply those requirements when writing or assessing the paper. "
        "They take precedence over general physics-journal conventions.\n\n"
    )


def project_prompt(template: str, *, investigating: bool, paper_mode: bool,
                   **instructions: str) -> str:
    task = find_task(ROOT)
    if investigating and task is None:
        raise RuntimeError("Restore TASK.md or task.md to resume the investigation.")
    instructions["task_instruction"] = (
        f"Read {task.name} and use it as the project brief." if task else ""
    )
    if not paper_mode and not investigating and task and (ROOT / "research.md").is_file():
        instructions["task_instruction"] += (
            " Use research.md and its supporting evidence to write or assess the paper."
        )
    # Older or customized templates can omit placeholders. Keep the handoff
    # visible to both roles in that case.
    missing = [
        value for key, value in instructions.items()
        if value and "{{" + key + "}}" not in template
    ]
    return render_prompt(
        "\n".join([*missing, template]), pdf_path=pdf_project_path(), **instructions,
    )


def run_researcher(iteration: int, *, investigating: bool = False, paper_mode: bool = False) -> str:
    feedback_instruction = (
        "Read feedback.md and reviewer-feedback.md when present. "
        "Supervisor feedback takes priority if the two conflict."
    )
    if FEEDBACK.exists():
        feedback_instruction += """

Read feedback.md carefully.

Address every CRITICAL and MAJOR issue.
Address MINOR issues when appropriate."""

    if REVIEWER_FEEDBACK.exists():
        feedback_instruction += """

Read reviewer-feedback.md carefully. Address outstanding referee requests
that apply to your current phase of work."""

    if COMMANDS is None:
        raise RuntimeError("Agent commands have not been configured")

    name = "investigation_prompt" if investigating else "researcher_prompt"
    prompt = project_prompt(
        prompt_for(COMMANDS, name), investigating=investigating, paper_mode=paper_mode,
        feedback_instruction=feedback_instruction,
    )
    label = "researcher (investigation)" if investigating else "researcher"
    if not investigating:
        prompt = journal_instruction(COMMANDS.journal, paper_mode=paper_mode) + prompt

    return run_with_capacity_retry(
        [*COMMANDS.researcher, prompt],
        label=label, terminal_label=f"{label} iteration {iteration}",
    )


def run_supervisor(iteration: int, *, investigating: bool = False, paper_mode: bool = False) -> str:
    if COMMANDS is None:
        raise RuntimeError("Agent commands have not been configured")

    reviewer_instruction = (
        "Read reviewer-feedback.md as historical context. It may explain why an "
        "earlier draft was returned. Verify that the "
        "current evidence addresses the referee's substantive concerns."
        if REVIEWER_FEEDBACK.exists() else ""
    )
    name = "investigation_supervisor_prompt" if investigating else "supervisor_prompt"
    prompt = project_prompt(
        prompt_for(COMMANDS, name), investigating=investigating, paper_mode=paper_mode,
        reviewer_feedback_instruction=reviewer_instruction,
    )
    label = "supervisor (investigation)" if investigating else "supervisor"
    if not investigating:
        prompt = journal_instruction(COMMANDS.journal, paper_mode=paper_mode) + prompt
        if not paper_mode and find_task(ROOT):
            prompt += (
                "An additional decision is available for this task. If a scientific "
                "gap requires further investigation, specify the work in feedback.md "
                "and end with STATUS: RESEARCH. Use REVISE for manuscript changes.\n"
            )
    return run_with_capacity_retry(
        [*COMMANDS.supervisor, prompt],
        label=label, terminal_label=f"{label} iteration {iteration}",
        result_path=FEEDBACK,
    )


def run_reviewer(iteration: int, *, paper_mode: bool = False) -> str:
    if COMMANDS is None:
        raise RuntimeError("Agent commands have not been configured")
    reviewer_prompt = prompt_for(COMMANDS, "reviewer_prompt")
    prompt = journal_instruction(COMMANDS.journal, paper_mode=paper_mode) + render_prompt(
        reviewer_prompt, pdf_path=pdf_project_path(),
    )
    return run_with_capacity_retry(
        [*reviewer_command_for(COMMANDS), prompt],
        label="reviewer",
        terminal_label=f"reviewer iteration {iteration}",
        result_path=REVIEWER_FEEDBACK,
    )


def validate_research() -> None:
    record = ROOT / "research.md"
    if not record.is_file() or not record.read_text(encoding="utf-8").strip():
        raise RuntimeError("Researcher must record the investigation in research.md")


def advance(step: str, iteration: int, *, paper_mode: bool) -> str:
    """Run one turn. Only a validated result advances the checkpoint."""
    if step == "investigate":
        run_researcher(iteration, investigating=True)
        validate_research()
        commit_researcher_changes(iteration, investigating=True)
        return "assess"

    if step == "assess":
        validate_research()
        FEEDBACK.unlink(missing_ok=True)
        run_supervisor(iteration, investigating=True)
        decision = validate_decision(FEEDBACK, "Supervisor", {"CONTINUE", "WRITE"})
        print(f"Research supervisor: {decision}")
        return "write" if decision == "WRITE" else "investigate"

    if step == "write":
        run_researcher(iteration, paper_mode=paper_mode)
        validate_pdf()
        commit_researcher_changes(iteration)
        return "supervise"

    validate_pdf()
    # A fresh supervisor or referee must never inherit its own partial report.
    FEEDBACK.unlink(missing_ok=True)
    if step == "supervise":
        run_supervisor(iteration, paper_mode=paper_mode)
        transitions = {"READY": "review", "REVISE": "write"}
        if not paper_mode:
            transitions["RESEARCH"] = "investigate"
        decision = validate_decision(FEEDBACK, "Supervisor", set(transitions))
        print(f"Manuscript supervisor: {decision}")
        return transitions[decision]

    REVIEWER_FEEDBACK.unlink(missing_ok=True)
    run_reviewer(iteration, paper_mode=paper_mode)
    decision = validate_reviewer_feedback()
    print(f"Independent reviewer: {decision}")
    return "accepted" if decision == "ACCEPT" else "write"


def main(
    project_root: Path,
    commands: AgentCommands,
    *,
    timeout_seconds: Optional[float] = None,
    heartbeat_seconds: float = 30.0,
    verbose_output: bool = False,
    paper_mode: bool = False,
    status_gist: Optional[str] = None,
) -> None:
    global ROOT, PAPER, FEEDBACK, REVIEWER_FEEDBACK, COMMANDS
    global AGENT_TIMEOUT_SECONDS, HEARTBEAT_SECONDS, STREAM_AGENT_OUTPUT, DASHBOARD

    ROOT = Path(project_root).resolve()
    FEEDBACK = ROOT / "feedback.md"
    REVIEWER_FEEDBACK = ROOT / "reviewer-feedback.md"
    COMMANDS = commands
    AGENT_TIMEOUT_SECONDS = timeout_seconds
    HEARTBEAT_SECONDS = heartbeat_seconds
    STREAM_AGENT_OUTPUT = verbose_output

    with project_checkpoint(ROOT) as checkpoint:
        mode = "paper" if paper_mode else "research"
        task = find_task(ROOT)
        saved = load_checkpoint(checkpoint, mode, "research" if task else "paper")
        if not task and not paper_mode:
            raise RuntimeError("Research mode needs TASK.md or task.md. Use --paper for a manuscript-only run.")
        if saved is None:
            step = "write" if paper_mode else "investigate"
            PAPER = select_pdf(ROOT)
            iteration = 1
            save_checkpoint(checkpoint, step, PAPER.relative_to(ROOT), mode, iteration)
        else:
            step, relative_pdf, iteration = saved
            PAPER = ROOT / relative_pdf
            print(f"Resuming {step} for {relative_pdf}.")

        dashboard = RunDashboard(checkpoint.parent, mode, status_gist)
        print(f"{mode.capitalize()} mode. Dashboard: {dashboard.path}")
        last_iteration = iteration + MAX_ITERATIONS - 1
        DASHBOARD = dashboard
        try:
            while iteration <= last_iteration:
                print(f"\n=== {step} (iteration {iteration}) ===")
                dashboard.update(step, iteration, "Running")
                next_step = advance(step, iteration, paper_mode=paper_mode)
                if next_step == "accepted":
                    checkpoint.unlink()
                    dashboard.update(step, iteration, "Accepted")
                    print("\nReviewer accepted the paper.")
                    return
                if next_step in ("investigate", "write"):
                    iteration += 1
                step = next_step
                save_checkpoint(checkpoint, step, PAPER.relative_to(ROOT), mode, iteration)
            raise RuntimeError(f"Maximum iterations ({MAX_ITERATIONS}) reached.")
        except (Exception, KeyboardInterrupt) as error:
            status = "Interrupted" if isinstance(error, KeyboardInterrupt) else "Stopped"
            dashboard.update(step, iteration, status)
            raise
        finally:
            DASHBOARD = None
