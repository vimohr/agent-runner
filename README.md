# Agent Runner

`agent-run` starts a researcher/reviewer agent loop in a project directory.
It only acts on directories containing `paper.pdf` or `TASK.md`.

## Requirements

- Python 3.10 or newer
- Git
- The `claude` and `codex` command-line tools, installed and authenticated

## Install from GitHub

Once this repository is pushed to GitHub, install it with
[pipx](https://pipx.pypa.io/) (recommended for command-line applications):

```sh
pipx install git+https://github.com/vimohr/agent-runner.git
```

Or with `uv`:

```sh
uv tool install git+https://github.com/vimohr/agent-runner.git
```

To upgrade later:

```sh
pipx upgrade agent-runner
```

You can also use regular pip:

```sh
python3 -m pip install git+https://github.com/vimohr/agent-runner.git
```

For local development, clone the repository and run:

```sh
python3 -m pip install -e .
```

## Usage

On the first run for a marked project, `agent-run` creates an `agent-run.json`
file containing the standard researcher and supervisor commands. It prints
the file location and asks whether to continue with the standard settings.
Answer `yes` to run immediately, or `no` (the default) to stop before any Git
or agent commands run so you can edit the file. Later runs preserve and reuse
your configuration without asking again.

The configured command is run directly, and the orchestrator's fixed prompt
is appended as its final argument.

For example:

```json
{
  "researcher": [
    "claude", "-p", "--model", "claude-opus-4-8", "--effort", "max"
  ],
  "supervisor": [
    "codex", "exec", "--model", "gpt-6-astra",
    "--config", "model_reasoning_effort=\"max\"",
    "--sandbox", "workspace-write"
  ]
}
```

Each value may alternatively be a shell-style command string, but argument
arrays are recommended because their quoting is unambiguous. Shell features
such as pipes and redirection are not interpreted.

Then run:

```sh
agent-run path/to/project
```

`agent-run` exits without changing anything unless the directory contains
`paper.pdf` or `TASK.md`. When a marker exists, it creates or validates
`agent-run.json` before making Git changes. With valid configuration, it
initializes a Git repository if necessary, creates or switches to the `agent`
branch, and runs the orchestrator with that directory as its project root.

You can also run it as a Python module:

```sh
python3 -m agent_runner path/to/project
```

## Development

Run the test suite with:

```sh
python3 -m unittest discover -s tests
```
