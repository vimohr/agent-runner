"""Local subprocess stand-in for the research-to-paper CLI integration test."""

from pathlib import Path
import os
import sys


role, prompt = sys.argv[1:]
assert "AGENT_RUN_GIST_TOKEN" not in os.environ
assert "On every turn, conduct your own thorough literature search" in prompt
log = Path(".agent-run/turns")
history = log.read_text().splitlines() if log.exists() else []
feedback = Path("feedback.md")
record = Path("research.md")
pdf = Path("paper.pdf")
investigating = "You are the RESEARCHER investigating" in prompt or (
    "You are the SUPERVISOR assessing the investigation" in prompt
)
if role != "reviewer":
    assert any(f"Read {name}" in prompt and Path(name).is_file() for name in ("task.md", "TASK.md"))
if investigating:
    assert not pdf.exists()
    assert "Target journal" not in prompt
    if role == "researcher":
        if record.exists():
            assert "Check route B" in feedback.read_text()
            record.write_text("Routes A and B checked. See counts.csv.\n")
            Path("counts.csv").write_text("route,count\nA,20\nB,32\n")
        else:
            record.write_text("Route A checked. Route B outstanding.\n")
    elif role == "supervisor":
        assert not feedback.exists()
        if "Route B outstanding" in record.read_text():
            feedback.write_text("Check route B.\nSTATUS: CONTINUE\n")
        else:
            feedback.write_text("Both routes checked.\nSTATUS: WRITE\n")
    turn = "investigation " + role
else:
    assert "Target journal: Journal of Testing" in prompt
    if role == "researcher":
        assert "STATUS: WRITE" in feedback.read_text()
        assert "research.md" in prompt
        assert "Routes A and B checked" in record.read_text()
        if "interrupted writer" not in history:
            with log.open("a") as stream:
                stream.write("interrupted writer\n")
            sys.exit(1)
        Path("paper.tex").write_text("Both routes were checked.")
        pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 1000)
    elif role == "supervisor":
        assert pdf.exists() and not feedback.exists()
        feedback.write_text("STATUS: READY\n")
    elif role == "reviewer":
        assert pdf.exists() and not feedback.exists()
        assert not Path("reviewer-feedback.md").exists()
        Path("reviewer-feedback.md").write_text("STATUS: ACCEPT\n")
    turn = "paper " + role

with log.open("a") as stream:
    stream.write(turn + "\n")
