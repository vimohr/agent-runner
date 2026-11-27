"""Persist the next agent turn and manuscript path across runner invocations."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
from typing import Iterator, Optional

from .documents import PDF_FILENAMES


@contextmanager
def project_checkpoint(root: Path) -> Iterator[Path]:
    directory = root / ".agent-run"
    directory.mkdir(exist_ok=True)
    # The OS releases the lock even if the runner is killed. Leave the lock
    # file in place so that all invocations lock the same inode.
    with (directory / "workflow.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError) as error:
            raise RuntimeError(f"Another runner is active in {root}") from error
        yield directory / "workflow.json"


def load_checkpoint(path: Path) -> Optional[tuple[str, Path]]:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        step = state["step"]
        pdf = Path(state["pdf"])
        if step not in ("investigate", "assess", "write", "supervise", "review"):
            raise ValueError("unknown workflow step")
        if pdf.is_absolute() or ".." in pdf.parts or pdf.name not in PDF_FILENAMES:
            raise ValueError("invalid manuscript path")
    except (TypeError, KeyError, ValueError) as error:
        raise RuntimeError(f"Invalid workflow checkpoint {path}: {error}") from error
    return step, pdf


def save_checkpoint(path: Path, step: str, pdf: Path) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"step": step, "pdf": pdf.as_posix()}) + "\n", encoding="utf-8",
    )
    temporary.replace(path)
