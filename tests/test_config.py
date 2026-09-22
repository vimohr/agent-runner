import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runner.config import (
    AgentCommands,
    ConfigurationError,
    default_config_text,
    ensure_agent_config,
    load_agent_commands,
)
from agent_runner import orchestrator


class ConfigurationTests(unittest.TestCase):
    def test_example_is_valid_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "agent-run.json").write_text(default_config_text())
            commands = load_agent_commands(folder)
            self.assertEqual(commands.researcher[0], "claude")
            self.assertEqual(commands.supervisor[:2], ("codex", "exec"))

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

    def test_string_commands_are_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "agent-run.json").write_text(json.dumps({
                "researcher": "author --model 'model with spaces'",
                "supervisor": "reviewer --strict",
            }))
            commands = load_agent_commands(folder)
            self.assertEqual(
                commands.researcher,
                ("author", "--model", "model with spaces"),
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


if __name__ == "__main__":
    unittest.main()
