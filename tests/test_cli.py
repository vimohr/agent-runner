import json
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runner import cli


class AgentRunTests(unittest.TestCase):
    @staticmethod
    def write_config(folder):
        (folder / "agent-run.json").write_text(json.dumps({
            "researcher": ["research-agent", "--model", "author-model"],
            "supervisor": ["review-agent", "--model", "reviewer-model"],
        }))

    def test_folder_without_marker_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch.object(cli, "switch_to_agent_branch") as switch:
                self.assertEqual(cli.main([str(folder)]), 0)
                switch.assert_not_called()
            self.assertFalse((folder / ".git").exists())

    def test_task_starts_orchestrator(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "TASK.md").write_text("Write a paper")
            self.write_config(folder)
            with patch.object(cli, "switch_to_agent_branch") as switch, \
                    patch.object(cli.orchestrator, "main") as run:
                self.assertEqual(cli.main([str(folder)]), 0)
                switch.assert_called_once_with(folder.resolve())
                commands = run.call_args.args[1]
                self.assertEqual(
                    commands.researcher,
                    ("research-agent", "--model", "author-model"),
                )
                self.assertEqual(
                    commands.supervisor,
                    ("review-agent", "--model", "reviewer-model"),
                )

    def test_missing_config_is_created_and_standard_settings_are_used(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "TASK.md").write_text("Write a paper")
            stdout = io.StringIO()
            with patch("sys.stdout", stdout), \
                    patch("builtins.input", return_value="yes"), \
                    patch.object(cli, "switch_to_agent_branch") as switch, \
                    patch.object(cli.orchestrator, "main") as run:
                self.assertEqual(cli.main([str(folder)]), 0)
                switch.assert_called_once_with(folder.resolve())
                run.assert_called_once()
            self.assertIn("Created", stdout.getvalue())
            self.assertTrue((folder / "agent-run.json").is_file())

    def test_declining_default_config_stops_before_git_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "TASK.md").write_text("Write a paper")
            stdout = io.StringIO()
            with patch("sys.stdout", stdout), \
                    patch("builtins.input", return_value="no"), \
                    patch.object(cli, "switch_to_agent_branch") as switch, \
                    patch.object(cli.orchestrator, "main") as run:
                self.assertEqual(cli.main([str(folder)]), 0)
                switch.assert_not_called()
                run.assert_not_called()
            self.assertIn("Stopped", stdout.getvalue())
            self.assertTrue((folder / "agent-run.json").is_file())

    def test_invalid_config_stops_before_git_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "TASK.md").write_text("Write a paper")
            (folder / "agent-run.json").write_text("{}")
            stderr = io.StringIO()
            with patch("sys.stderr", stderr), \
                    patch.object(cli, "switch_to_agent_branch") as switch:
                self.assertEqual(cli.main([str(folder)]), 2)
                switch.assert_not_called()
            self.assertIn("Fix", stderr.getvalue())
            self.assertFalse((folder / ".git").exists())

    def test_new_repository_starts_on_agent_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            cli.switch_to_agent_branch(folder)
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=folder,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(branch, "agent")


if __name__ == "__main__":
    unittest.main()
