import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runner import cli, orchestrator
from agent_runner.checkpoint import project_checkpoint
from agent_runner.config import AgentCommands
from agent_runner.dashboard import RunDashboard


class ReviewLoopTests(unittest.TestCase):
    def test_task_to_review_through_cli_and_resume(self):
        agent = Path(__file__).resolve().parent / "fixtures/investigation_agent.py"
        identity = {
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, identity):
            root = Path(temporary)
            (root / "task.md").write_text("Compare route A and route B.")
            with patch("builtins.input", return_value="no"):
                self.assertEqual(cli.main([temporary]), 0)
            config = root / "agent-run.json"
            settings = json.loads(config.read_text())
            settings["journal"] = "Journal of Testing"
            for role in ("researcher", "supervisor", "reviewer"):
                settings[role] = [sys.executable, str(agent), role]
            config.write_text(json.dumps(settings))

            self.assertEqual(cli.main([temporary]), 1)
            self.assertFalse((root / "paper.pdf").exists())
            state = root / ".agent-run/workflow.json"
            self.assertEqual(json.loads(state.read_text())["step"], "write")
            stopped = (root / ".agent-run/dashboard.html").read_text()
            self.assertIn('"status": "Stopped"', stopped)
            self.assertEqual(cli.main([temporary, "--paper"]), 1)
            self.assertEqual((root / ".agent-run/dashboard.html").read_text(), stopped)
            with patch.dict(os.environ, {"AGENT_RUN_GIST_TOKEN": "test-gist-token"}), \
                    patch.object(cli.shutil, "which", return_value=None), \
                    patch("agent_runner.dashboard.urlopen") as send:
                self.assertEqual(cli.main([temporary, "--status-gist", "abc123"]), 0)
            request = send.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.github.com/gists/abc123")
            self.assertEqual(request.get_method(), "PATCH")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-gist-token")
            snapshot = json.loads(request.data)["files"]["status.json"]["content"]
            self.assertEqual(json.loads(snapshot)["events"][-1]["status"], "Accepted")
            self.assertNotIn("test-gist-token", snapshot)
            self.assertFalse(state.exists())
            self.assertIn('"status": "Accepted"', (root / ".agent-run/dashboard.html").read_text())
            self.assertEqual((root / ".agent-run/turns").read_text().splitlines(), [
                "investigation researcher", "investigation supervisor",
                "investigation researcher", "investigation supervisor",
                "interrupted writer", "paper researcher", "paper supervisor", "paper reviewer",
            ])
            tracked = subprocess.check_output(["git", "ls-files"], cwd=root, text=True)
            self.assertEqual(set(tracked.splitlines()), {
                "task.md", "research.md", "counts.csv", "paper.tex", "paper.pdf",
            })
            self.assertEqual((root / "reviewer-feedback.md").read_text(), "STATUS: ACCEPT\n")

    def test_referee_evidence_gap_returns_to_investigation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "TASK.md").write_text("Investigate the claim in the existing draft.")
            pdf = root / "main.pdf"
            pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 1000)
            commands = AgentCommands(researcher=("author",), supervisor=("supervisor",))
            reports = iter(("WRITE", "READY", "REVISE", "RESEARCH", "WRITE", "READY", "ACCEPT"))
            calls = []

            def fake_run(command, *, label, terminal_label):
                calls.append(label)
                if label == "researcher (investigation)":
                    (root / "research.md").write_text("Evidence for the claim.")
                    if len(calls) > 1:
                        self.assertIn("RESEARCH", (root / "feedback.md").read_text())
                        self.assertIn("REVISE", (root / "reviewer-feedback.md").read_text())
                elif label == "reviewer":
                    self.assertFalse((root / "feedback.md").exists())
                    self.assertFalse((root / "reviewer-feedback.md").exists())
                    (root / "reviewer-feedback.md").write_text(f"STATUS: {next(reports)}")
                elif label.startswith("supervisor"):
                    self.assertFalse((root / "feedback.md").exists())
                    (root / "feedback.md").write_text(f"STATUS: {next(reports)}")
                return ""

            with patch.object(orchestrator, "run", side_effect=fake_run), \
                    patch.object(orchestrator, "commit_researcher_changes"):
                orchestrator.main(root, commands)
            self.assertEqual(calls, [
                "researcher (investigation)", "supervisor (investigation)",
                "researcher", "supervisor", "reviewer", "researcher", "supervisor",
                "researcher (investigation)", "supervisor (investigation)",
                "researcher", "supervisor", "reviewer",
            ])

    def test_missing_evidence_or_invalid_approval_keeps_the_pending_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "TASK.md").write_text("Study the question.")
            commands = AgentCommands(researcher=("author",), supervisor=("supervisor",))
            state = root / ".agent-run/workflow.json"
            with patch.object(orchestrator, "run", return_value=""):
                with self.assertRaisesRegex(RuntimeError, "research.md"):
                    orchestrator.main(root, commands)
            self.assertEqual(json.loads(state.read_text())["step"], "investigate")
            (root / "research.md").write_text("A preliminary result.")

            def stale_approval(*args, **kwargs):
                (root / "feedback.md").write_text("STATUS: READY")

            with patch.object(orchestrator, "run", side_effect=stale_approval), \
                    patch.object(orchestrator, "commit_researcher_changes"):
                with self.assertRaisesRegex(RuntimeError, "STATUS: WRITE"):
                    orchestrator.main(root, commands)
            self.assertEqual(json.loads(state.read_text())["step"], "assess")
            with project_checkpoint(root), self.assertRaisesRegex(RuntimeError, "active"):
                orchestrator.main(root, commands)

    def test_referee_revisions_return_to_researcher_and_supervisor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "TASK.md").write_text("Polish the manuscript")
            (root / "paper.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 1000)
            commands = AgentCommands(
                researcher=("author",), supervisor=("supervisor",),
                reviewer=("referee",),
                journal="Physical Review Letters",
                researcher_prompt=(
                    "Write {{pdf_path}}.\n"
                    "Do a thorough literature analysis for context when appropriate."
                ),
                supervisor_prompt="Review {{pdf_path}}.",
            )
            supervisor_decisions = iter(("REVISE", "READY", "READY", "READY"))
            reviewer_decisions = iter(("REJECT", "REVISE", "ACCEPT"))
            calls = []

            def fake_run(command, *, label, terminal_label):
                role, prompt = command[0], command[-1]
                calls.append((role, prompt))
                self.assertIn("iteration", terminal_label)
                self.assertNotIn("iteration", label)
                self.assertTrue(prompt.startswith(
                    "IMPORTANT — Target journal: Physical Review Letters."
                ))
                self.assertIn("Before starting, consult", prompt)
                self.assertIn("conduct your own thorough literature search", prompt)
                self.assertNotIn("literature analysis for context when appropriate", prompt)
                if role == "supervisor":
                    self.assertNotIn("STATUS: RESEARCH", prompt)
                    self.assertFalse((root / "feedback.md").exists())
                    if (root / "reviewer-feedback.md").exists():
                        self.assertIn("why an earlier draft", prompt)
                    (root / "feedback.md").write_text(
                        f"# Supervisor Review\nSTATUS: {next(supervisor_decisions)}\n"
                    )
                elif role == "referee":
                    self.assertFalse((root / "reviewer-feedback.md").exists())
                    self.assertFalse((root / "feedback.md").exists())
                    (root / "reviewer-feedback.md").write_text(
                        f"# Referee Report\nSTATUS: {next(reviewer_decisions)}\n"
                    )
                return ""

            with patch.object(orchestrator, "run", side_effect=fake_run), \
                    patch.object(orchestrator, "commit_researcher_changes"):
                orchestrator.main(root, commands, paper_mode=True)

            self.assertEqual(
                [role for role, _ in calls],
                ["author", "supervisor", "author", "supervisor", "referee",
                 "author", "supervisor", "referee", "author", "supervisor",
                 "referee"],
            )
            self.assertIn("reviewer-feedback.md", calls[5][1])
            self.assertIn("reviewer-feedback.md", calls[6][1])
            self.assertIn("Supervisor feedback takes priority", calls[5][1])
            self.assertEqual(
                (root / "reviewer-feedback.md").read_text().splitlines()[-1],
                "STATUS: ACCEPT",
            )

    def test_gist_contains_only_progress_and_heartbeat_is_throttled(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            dashboard = RunDashboard(folder, "paper", "abc123")
            with patch.dict(os.environ, {"AGENT_RUN_GIST_TOKEN": ""}), \
                    patch("agent_runner.dashboard.subprocess.run") as gh:
                dashboard.update("write", 4, "Running")
                dashboard.heartbeat()
                self.assertEqual(gh.call_count, 1)
                command = gh.call_args.args[0]
                self.assertIn("gists/abc123", command)
                payload = json.loads(gh.call_args.kwargs["input"])
                snapshot = json.loads(payload["files"]["status.json"]["content"])
                self.assertEqual(set(snapshot), {"mode", "events", "updated"})
                event, = snapshot["events"]
                self.assertEqual(set(event), {"phase", "agent", "round", "time", "status"})
                self.assertEqual(event["agent"], "Researcher")
                self.assertEqual(snapshot["mode"], "paper")
                gh.side_effect = subprocess.TimeoutExpired(command, 10)
                dashboard.update("review", 5, "Accepted")
            self.assertIn('"status": "Accepted"', dashboard.path.read_text())

    def test_researcher_reads_both_reports_with_supervisor_priority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "feedback.md").write_text("Supervisor requests A")
            (root / "reviewer-feedback.md").write_text("Referee requests B")
            commands = AgentCommands(
                researcher=("author",), supervisor=("supervisor",),
                reviewer=("referee",),
            )
            with patch.object(orchestrator, "ROOT", root), \
                    patch.object(orchestrator, "PAPER", root / "paper.pdf"), \
                    patch.object(orchestrator, "FEEDBACK", root / "feedback.md"), \
                    patch.object(orchestrator, "REVIEWER_FEEDBACK", root / "reviewer-feedback.md"), \
                    patch.object(orchestrator, "COMMANDS", commands), \
                    patch.object(orchestrator, "run", return_value="") as run:
                orchestrator.run_researcher(2)
            prompt = run.call_args.args[0][-1]
            self.assertIn("Read feedback.md carefully", prompt)
            self.assertIn("Read reviewer-feedback.md carefully", prompt)
            self.assertIn("Supervisor feedback takes priority", prompt)

    def test_decision_must_be_unique_final_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviewer-feedback.md"
            path.write_text("STATUS: REVISE\nSTATUS: ACCEPT\n")
            with self.assertRaisesRegex(RuntimeError, "exactly one STATUS"):
                orchestrator.validate_decision(path, "Reviewer", {"ACCEPT", "REVISE"})
            path.write_text("STATUS: ACCEPT\nMore comments\n")
            with self.assertRaisesRegex(RuntimeError, "must end"):
                orchestrator.validate_decision(path, "Reviewer", {"ACCEPT", "REVISE"})


if __name__ == "__main__":
    unittest.main()
