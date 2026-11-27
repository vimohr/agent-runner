"""Render a local status page and optionally update a GitHub Gist feed."""

from datetime import datetime, timezone
from importlib import resources
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Optional


TURNS = {
    "investigate": ("Investigation", "Researcher"),
    "assess": ("Investigation", "Supervisor"),
    "write": ("Manuscript", "Researcher"),
    "supervise": ("Manuscript", "Supervisor"),
    "review": ("Independent review", "Reviewer"),
}


class RunDashboard:
    def __init__(self, directory: Path, mode: str, gist: Optional[str]):
        self.path = directory / "dashboard.html"
        self.mode = mode
        self.gist = gist
        self.events: list[dict] = []
        self.last_publish = 0.0
        self.template = resources.files("agent_runner").joinpath("index.html").read_text(
            encoding="utf-8",
        )

    def update(self, step: str, iteration: int, status: str) -> None:
        phase, agent = TURNS[step]
        event = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "phase": phase, "agent": agent, "round": iteration, "status": status,
        }
        self.events.append(event)
        self.write(publish=True)

    def heartbeat(self) -> None:
        self.write(publish=time.monotonic() - self.last_publish >= 300)

    def write(self, *, publish: bool) -> None:
        snapshot = json.dumps({
            "mode": self.mode, "events": self.events[-20:],
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        try:
            temp = self.path.with_suffix(".tmp.html")
            temp.write_text(
                self.template.replace('id="snapshot">null', 'id="snapshot">' + snapshot),
                encoding="utf-8",
            )
            temp.replace(self.path)
        except OSError as error:
            print(f"Could not update local dashboard: {error}", file=sys.stderr)
        if self.gist and publish:
            self.last_publish = time.monotonic()
            payload = {"files": {"status.json": {"content": snapshot}}}
            try:
                subprocess.run(
                    ["gh", "api", "--method", "PATCH", f"gists/{self.gist}", "--input", "-"],
                    input=json.dumps(payload), text=True, capture_output=True,
                    check=True, timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as error:
                print(f"Could not update dashboard Gist: {error}", file=sys.stderr)
