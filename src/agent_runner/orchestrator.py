"""Researcher and supervisor agent orchestration loop."""

from pathlib import Path
import subprocess
import sys
from typing import Optional, Sequence

from . import __version__
from .config import AgentCommands
from .documents import find_existing_pdf


ROOT = Path.cwd()
PAPER = ROOT / "paper.pdf"
FEEDBACK = ROOT / "feedback.md"

# Allow some back-and-forth while still bounding a runaway review cycle.
MAX_ITERATIONS = 100

COMMANDS: Optional[AgentCommands] = None


def select_pdf(project_root: Path) -> Path:
    # TASK.md-only projects continue to create paper.pdf at the root.
    return find_existing_pdf(project_root) or project_root / "paper.pdf"


def pdf_project_path() -> str:
    return PAPER.relative_to(ROOT).as_posix()


def run(cmd: Sequence[str], cwd: Optional[Path] = None) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd or ROOT,
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: {cmd}"
        )

    return result.stdout


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

    return run([*COMMANDS.researcher, prompt])


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

    return run([*COMMANDS.supervisor, prompt])


def main(project_root: Path, commands: AgentCommands) -> None:
    global ROOT, PAPER, FEEDBACK, COMMANDS

    ROOT = Path(project_root).resolve()
    PAPER = select_pdf(ROOT)
    FEEDBACK = ROOT / "feedback.md"
    COMMANDS = commands

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
