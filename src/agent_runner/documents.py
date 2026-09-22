"""Document discovery rules shared by the launcher and orchestrator."""

from pathlib import Path
from typing import Optional


PDF_FILENAMES = ("paper.pdf", "main.pdf")


def find_existing_pdf(project_root: Path) -> Optional[Path]:
    """Find the preferred PDF recursively within a project.

    ``paper.pdf`` takes precedence over ``main.pdf``. If multiple files have
    the preferred name, the shallowest path wins, followed by lexical order.
    """
    for filename in PDF_FILENAMES:
        candidates = [
            path for path in project_root.rglob(filename) if path.is_file()
        ]
        if candidates:
            return min(
                candidates,
                key=lambda path: (
                    len(path.relative_to(project_root).parts),
                    path.relative_to(project_root).as_posix(),
                ),
            )

    return None
