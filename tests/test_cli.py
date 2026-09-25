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

    def test_email_is_sent_after_successful_loop(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "TASK.md").write_text("Write a paper")
            self.write_config(folder)
            with patch.object(cli, "switch_to_agent_branch"), \
                    patch.object(cli.orchestrator, "main") as run, \
                    patch.object(cli, "check_email_delivery"), \
                    patch.object(cli, "send_completion_email") as send:
                self.assertEqual(
                    cli.main([str(folder), "--email=reader@example.com"]), 0,
                )
                run.assert_called_once()
                send.assert_called_once_with("reader@example.com", folder.resolve())

    def test_email_is_not_sent_when_loop_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "TASK.md").write_text("Write a paper")
            self.write_config(folder)
            with patch.object(cli, "switch_to_agent_branch"), \
                    patch.object(cli.orchestrator, "main", side_effect=RuntimeError("failed")), \
                    patch.object(cli, "check_email_delivery"), \
                    patch.object(cli, "send_completion_email") as send, \
                    patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    cli.main([str(folder), "--email=reader@example.com"]), 1,
                )
                send.assert_not_called()

    def test_completion_email_uses_sendmail(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "paper.pdf").write_bytes(b"pdf")
            with patch.dict(cli.os.environ, {}, clear=True), \
                    patch.object(cli.shutil, "which", return_value="/usr/sbin/sendmail"), \
                    patch.object(cli.subprocess, "run") as run:
                run.return_value.returncode = 0
                cli.send_completion_email("reader@example.com", folder)

            self.assertEqual(run.call_args.args[0], ["/usr/sbin/sendmail", "-t", "-i"])
            message = run.call_args.kwargs["input"].decode()
            self.assertIn("To: reader@example.com", message)
            self.assertIn("the external reviewer accepted it.", message)
            self.assertIn(str(folder / "paper.pdf"), message)

    def test_sendmail_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch.dict(cli.os.environ, {}, clear=True), \
                    patch.object(cli.shutil, "which", return_value="/usr/sbin/sendmail"), \
                    patch.object(cli.subprocess, "run") as run:
                run.return_value.returncode = 75
                run.return_value.stderr = b"delivery unavailable"
                with self.assertRaisesRegex(RuntimeError, "delivery unavailable"):
                    cli.send_completion_email("reader@example.com", folder)

    def test_completion_email_uses_cluster_mail_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "paper.pdf").write_bytes(b"pdf")

            def which(command):
                return "/usr/bin/mail" if command == "mail" else None

            with patch.dict(cli.os.environ, {}, clear=True), \
                    patch.object(cli.shutil, "which", side_effect=which), \
                    patch.object(cli.subprocess, "run") as run:
                run.return_value.returncode = 0
                cli.check_email_delivery()
                cli.send_completion_email("reader@example.com", folder)

            self.assertEqual(
                run.call_args.args[0],
                ["/usr/bin/mail", "-s", f"agent-run completed: {folder.name}",
                 "reader@example.com"],
            )
            self.assertIn(
                b"the external reviewer accepted it.",
                run.call_args.kwargs["input"],
            )

    def test_completion_email_uses_smtp_without_sendmail(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "paper.pdf").write_bytes(b"pdf")
            settings = {
                "AGENT_RUN_SMTP_HOST": "smtp.example.com",
                "AGENT_RUN_EMAIL_FROM": "agent@example.com",
                "AGENT_RUN_SMTP_USERNAME": "agent@example.com",
                "AGENT_RUN_SMTP_PASSWORD": "secret",
            }
            with patch.dict(cli.os.environ, settings, clear=True), \
                    patch.object(cli.shutil, "which", return_value=None), \
                    patch.object(cli.smtplib, "SMTP") as smtp_class:
                cli.check_email_delivery()
                cli.send_completion_email("reader@example.com", folder)

            smtp_class.assert_called_once_with("smtp.example.com", 587, timeout=30)
            smtp = smtp_class.return_value.__enter__.return_value
            smtp.starttls.assert_called_once()
            smtp.login.assert_called_once_with("agent@example.com", "secret")
            message = smtp.send_message.call_args.args[0]
            self.assertEqual(message["To"], "reader@example.com")
            self.assertEqual(message["From"], "agent@example.com")

    def test_smtp_configuration_is_checked_before_loop(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "TASK.md").write_text("Write a paper")
            self.write_config(folder)
            with patch.dict(cli.os.environ, {"AGENT_RUN_SMTP_HOST": "smtp.example.com"}, clear=True), \
                    patch.object(cli, "switch_to_agent_branch") as switch, \
                    patch.object(cli.orchestrator, "main") as run, \
                    patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    cli.main([str(folder), "--email=reader@example.com"]), 1,
                )
                switch.assert_not_called()
                run.assert_not_called()

    def test_invalid_email_address_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args([".", "--email=one@example.com,two@example.com"])

    def test_main_pdf_starts_orchestrator(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            nested = folder / "build" / "output"
            nested.mkdir(parents=True)
            (nested / "main.pdf").write_bytes(b"pdf")
            self.write_config(folder)
            with patch.object(cli, "switch_to_agent_branch") as switch, \
                    patch.object(cli.orchestrator, "main") as run:
                self.assertEqual(cli.main([str(folder)]), 0)
                switch.assert_called_once_with(folder.resolve())
                run.assert_called_once()

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
