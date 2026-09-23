import json
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runner.config import (
    AgentCommands,
    ConfigurationError,
    LEGACY_RESEARCHER_COMMIT_INSTRUCTION,
    default_config_text,
    ensure_agent_config,
    load_agent_commands,
)
from agent_runner import orchestrator


class ConfigurationTests(unittest.TestCase):
    def test_runner_commits_researcher_files_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=root, check=True,
                    capture_output=True, text=True,
                ).stdout.strip()

            git("init", "-q")
            git("config", "user.name", "Test Author")
            git("config", "user.email", "author@example.com")
            (root / "paper.tex").write_text("Initial paper")
            git("add", "paper.tex")
            git("commit", "-qm", "Initial paper")

            (root / "paper.tex").write_text("Revised paper")
            (root / "paper.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 1000)
            (root / "feedback.md").write_text("Old feedback")
            (root / "agent-run.json").write_text("{}")
            (root / ".agent-run").mkdir()
            (root / ".agent-run" / "agent-run.log").write_text("log")
            git("add", "feedback.md", "agent-run.json", ".agent-run/agent-run.log")

            with patch.object(orchestrator, "ROOT", root):
                orchestrator.commit_researcher_changes(2)
                first_commit = git("rev-parse", "HEAD")
                orchestrator.commit_researcher_changes(2)

            self.assertEqual(git("rev-parse", "HEAD"), first_commit)
            self.assertEqual(
                set(git("show", "--pretty=format:", "--name-only", "HEAD").splitlines()),
                {"paper.tex", "paper.pdf"},
            )
            self.assertEqual(
                git("log", "-1", "--format=%s"),
                "Researcher iteration 2: update paper",
            )
            self.assertEqual(
                set(git("diff", "--cached", "--name-only").splitlines()),
                {"feedback.md", "agent-run.json", ".agent-run/agent-run.log"},
            )

    def test_run_streams_and_logs_agent_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdout = io.StringIO()
            stderr = io.StringIO()
            command = [
                sys.executable,
                "-c",
                "import sys; print('visible out'); print('visible err', file=sys.stderr)",
                "test prompt",
            ]
            with patch.object(orchestrator, "ROOT", root), \
                    patch.object(orchestrator, "AGENT_TIMEOUT_SECONDS", None), \
                    patch.object(orchestrator, "HEARTBEAT_SECONDS", 30), \
                    patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                result = orchestrator.run(command, label="test agent")

            self.assertEqual(result, "visible out\n")
            self.assertIn("visible out", stdout.getvalue())
            self.assertIn("visible err", stderr.getvalue())
            log = (root / ".agent-run" / "agent-run.log").read_text()
            self.assertIn("test agent", log)
            self.assertIn("[stdout] visible out", log)
            self.assertIn("[stderr] visible err", log)
            self.assertNotIn("test prompt", log)

    def test_run_timeout_stops_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [sys.executable, "-c", "import time; time.sleep(10)", "prompt"]
            with patch.object(orchestrator, "ROOT", root), \
                    patch.object(orchestrator, "AGENT_TIMEOUT_SECONDS", 0.1), \
                    patch.object(orchestrator, "HEARTBEAT_SECONDS", 30), \
                    patch("sys.stdout", io.StringIO()), \
                    patch("sys.stderr", io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "exceeded"):
                    orchestrator.run(command, label="slow agent")

    def test_example_is_valid_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "agent-run.json").write_text(default_config_text())
            commands = load_agent_commands(folder)
            self.assertEqual(commands.researcher[0], "claude")
            self.assertEqual(commands.supervisor[:2], ("codex", "exec"))
            self.assertIn("{{iteration}}", commands.researcher_prompt)
            self.assertIn("{{pdf_path}}", commands.supervisor_prompt)
            self.assertNotIn("commit", commands.researcher_prompt.lower())

    def test_default_configuration_can_be_materialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            path, created = ensure_agent_config(folder)
            self.assertTrue(created)
            self.assertEqual(path.name, "agent-run.json")
            self.assertEqual(path.read_text(), default_config_text())
            same_path, created_again = ensure_agent_config(folder)
            self.assertFalse(created_again)
            self.assertEqual(same_path, path)

    def test_old_generated_prompt_no_longer_requests_a_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "agent-run.json").write_text(json.dumps({
                "researcher": ["author"],
                "supervisor": ["reviewer"],
                "researcher_prompt": [
                    "Write the paper.",
                    LEGACY_RESEARCHER_COMMIT_INSTRUCTION,
                ],
            }))
            commands = load_agent_commands(folder)
            self.assertNotIn("commit", commands.researcher_prompt.lower())
            self.assertIn("Write the paper.", commands.researcher_prompt)

    def test_string_commands_are_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "TASK.md").write_text("Write a paper")
            (folder / "agent-run.json").write_text(json.dumps({
                "researcher": "author --model 'model with spaces'",
                "supervisor": "reviewer --strict",
            }))
            commands = load_agent_commands(folder)
            self.assertEqual(
                commands.researcher,
                ("author", "--model", "model with spaces"),
            )
            self.assertIn("RESEARCHER / AUTHOR", commands.researcher_prompt)

    def test_custom_prompts_are_loaded_and_rendered(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "TASK.md").write_text("Write a paper")
            (folder / "agent-run.json").write_text(json.dumps({
                "researcher": ["author"],
                "supervisor": ["reviewer"],
                "researcher_prompt": [
                    "Research pass {{iteration}}",
                    "Write {{pdf_path}}. {{task_instruction}}",
                ],
                "supervisor_prompt": "Review {{pdf_path}} on pass {{iteration}}",
            }))
            commands = load_agent_commands(folder)
            root = folder
            with patch.object(orchestrator, "COMMANDS", commands), \
                    patch.object(orchestrator, "ROOT", root), \
                    patch.object(orchestrator, "PAPER", root / "paper.pdf"), \
                    patch.object(orchestrator, "FEEDBACK", root / "feedback.md"), \
                    patch.object(orchestrator, "run", return_value="") as run:
                orchestrator.run_researcher(4)
                researcher_prompt = run.call_args.args[0][-1]
                self.assertIn("Research pass 4", researcher_prompt)
                self.assertIn("Write paper.pdf", researcher_prompt)
                self.assertIn("Read TASK.md", researcher_prompt)

                orchestrator.run_supervisor(5)
                supervisor_prompt = run.call_args.args[0][-1]
                self.assertEqual(
                    supervisor_prompt,
                    "Review paper.pdf on pass 5\n",
                )

    def test_invalid_command_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "agent-run.json").write_text(json.dumps({
                "researcher": [],
                "supervisor": ["reviewer"],
            }))
            with self.assertRaises(ConfigurationError):
                load_agent_commands(folder)

    def test_fixed_prompt_is_appended_to_configured_command(self):
        commands = AgentCommands(
            researcher=("author", "--model", "one"),
            supervisor=("reviewer", "--model", "two"),
        )
        with patch.object(orchestrator, "COMMANDS", commands), \
                patch.object(orchestrator, "run", return_value="") as run:
            orchestrator.run_researcher(3)
            command = run.call_args.args[0]
            self.assertEqual(command[:-1], list(commands.researcher))
            self.assertIn("This is iteration 3.", command[-1])

    def test_recursive_main_pdf_is_fallback_and_paper_pdf_has_priority(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            nested = folder / "documents" / "output"
            nested.mkdir(parents=True)
            main_pdf = nested / "main.pdf"
            main_pdf.write_bytes(b"main")
            self.assertEqual(orchestrator.select_pdf(folder), main_pdf)

            paper_pdf = nested / "paper.pdf"
            paper_pdf.write_bytes(b"paper")
            self.assertEqual(orchestrator.select_pdf(folder), paper_pdf)

    def test_main_pdf_name_is_used_in_supervisor_prompt(self):
        commands = AgentCommands(
            researcher=("author",),
            supervisor=("reviewer",),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main_pdf = root / "output" / "main.pdf"
            with patch.object(orchestrator, "COMMANDS", commands), \
                    patch.object(orchestrator, "ROOT", root), \
                    patch.object(orchestrator, "PAPER", main_pdf), \
                    patch.object(orchestrator, "run", return_value="") as run:
                orchestrator.run_supervisor(1)
                prompt = run.call_args.args[0][-1]
                self.assertIn("output/main.pdf", prompt)
                self.assertNotIn("paper.pdf", prompt)


if __name__ == "__main__":
    unittest.main()
