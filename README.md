# Agent Runner

`agent-run` starts a researcher/reviewer agent loop in a project directory.
It only acts on directories containing `paper.pdf` or `main.pdf` anywhere in
their directory tree, or a top-level `TASK.md`.

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
file containing the standard researcher and supervisor commands and prompts.
It prints the file location and asks whether to continue with the standard
settings.
Answer `yes` to run immediately, or `no` (the default) to stop before any Git
or agent commands run so you can edit the file. Later runs preserve and reuse
your configuration without asking again.

The configured command is run directly, and its configured prompt is appended
as the final argument. Prompts can be a string or an array of lines. Arrays
make longer prompts easier to edit in JSON.

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
  ],
  "researcher_prompt": [
    "You are the RESEARCHER / AUTHOR.",
    "This is iteration {{iteration}}.",
    "Write the completed paper to {{pdf_path}}.",
    "{{task_instruction}}",
    "{{feedback_instruction}}"
  ],
  "supervisor_prompt": [
    "You are the SUPERVISOR / REVIEWER.",
    "This is review iteration {{iteration}}.",
    "Critically review {{pdf_path}} and write feedback.md."
  ]
}
```

The complete generated file contains the standard prompts. The following
placeholders are replaced just before each agent starts:

- Both prompts: `{{iteration}}`, `{{pdf_path}}`
- Researcher only: `{{task_instruction}}`, `{{feedback_instruction}}`

Prompt fields are optional for compatibility with existing configurations;
when omitted, the standard prompt is used.

Each command may alternatively be a shell-style string, but argument arrays
are recommended because their quoting is unambiguous. Shell features such as
pipes and redirection are not interpreted.

Then run:

```sh
agent-run path/to/project
```

Agent output is shown live instead of being held until the command finishes.
While an agent is running, `agent-run` prints a heartbeat every 30 seconds with
its role, PID, and elapsed time. The complete output is also appended to
`.agent-run/agent-run.log` in the project, so a failed or interrupted run can
be inspected afterward.

To change the heartbeat interval or put a time limit on each individual agent:

```sh
agent-run --heartbeat 10 --timeout 3600 path/to/project
```

There is no timeout by default because a legitimate research or compilation
step may take a long time. Pressing Ctrl-C stops the current agent and its
child processes cleanly; the log remains available for diagnosis.

`agent-run` exits without changing anything unless the directory contains
`paper.pdf` or `main.pdf` anywhere below it, or a top-level `TASK.md`. When
both PDF names exist, `paper.pdf` takes precedence regardless of depth;
otherwise `main.pdf` is used throughout the researcher and supervisor
workflow. If multiple PDFs have the preferred name, the shallowest path wins,
with alphabetical path order breaking a tie. The relative path is included in
both agent prompts. A project containing only `TASK.md` creates `paper.pdf` at
the project root by default.

After each successful researcher run, `agent-run` validates the PDF and commits
project changes with an iteration-specific Git message. It skips the commit
when there are no changes, and excludes `feedback.md`, `agent-run.json`, and
`.agent-run` from its commits. The former commit instruction is ignored when
loading an older generated `agent-run.json`.

When a marker exists, `agent-run` creates or validates `agent-run.json` before
making Git changes. With valid configuration, it initializes a Git repository
if necessary, creates or switches to the `agent` branch, and runs the
orchestrator with that directory as its project root.

You can also run it as a Python module:

```sh
python3 -m agent_runner path/to/project
```

## Development

Run the test suite with:

```sh
python3 -m unittest discover -s tests
```
