import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runner import orchestrator
from agent_runner.config import AgentCommands


class ReviewLoopTests(unittest.TestCase):
    def test_referee_revisions_return_to_researcher_and_supervisor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "paper.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 1000)
            commands = AgentCommands(
                researcher=("author",), supervisor=("supervisor",),
                reviewer=("referee",),
                researcher_prompt="Write {{pdf_path}}.",
                supervisor_prompt="Review {{pdf_path}}.",
            )
            supervisor_decisions = iter(("REVISE", "READY", "READY", "READY"))
            reviewer_decisions = iter(("REJECT", "REVISE", "ACCEPT"))
            calls = []

            def fake_run(command, *, label):
                role, prompt = command[0], command[-1]
                calls.append((role, prompt))
                if role == "supervisor":
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
                orchestrator.main(root, commands)

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
