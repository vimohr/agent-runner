# Agent Runner

`agent-run` starts a researcher, supervisor, and independent reviewer loop in a project directory.
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
file containing the standard researcher, supervisor, and reviewer commands and prompts.
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
  "journal": "",
  "researcher": [
    "codex", "exec", "--model", "gpt-6-astra",
    "--config", "model_reasoning_effort=\"max\"",
    "--sandbox", "workspace-write"
  ],
  "supervisor": [
    "claude", "-p", "--model", "claude-opus-5-5", "--effort", "max"
  ],
  "reviewer": [
    "codex", "exec", "--model", "gpt-6-astra",
    "--config", "model_reasoning_effort=\"max\"",
    "--sandbox", "workspace-write"
  ],
  "researcher_prompt": [
    "You are the RESEARCHER / AUTHOR.",
    "Write the completed paper to {{pdf_path}}.",
    "{{task_instruction}}",
    "{{feedback_instruction}}"
  ],
  "supervisor_prompt": [
    "You are the SUPERVISOR.",
    "Critically review {{pdf_path}} and write feedback.md.",
    "{{reviewer_feedback_instruction}}"
  ],
  "reviewer_prompt": [
    "You are an independent referee for the target journal.",
    "Review {{pdf_path}} and write reviewer-feedback.md with STATUS: ACCEPT, REVISE, or REJECT."
  ]
}
```

The complete generated file contains the standard prompts. The following
placeholders are replaced just before each agent starts:

- All three prompts: `{{pdf_path}}`
- Researcher only: `{{task_instruction}}`, `{{feedback_instruction}}`
- Supervisor only: `{{reviewer_feedback_instruction}}`

Set `"journal"` to a specific title such as `"Physical Review Letters"` to
target it. Leave the field empty or omit it to use the general
`Physical Review style journal` target. Before every agent prompt, the runner
adds an instruction to consult the current official scope, author guidance,
paper style, and editorial criteria for that exact journal and apply them to
the work. The general target uses Physical Review family guidance. This
instruction also applies when you supply custom agent prompts.

Agents are not told the current iteration or the 100-iteration safety limit.
For compatibility with older project configurations, prompt lines containing
the former `{{iteration}}` placeholder are omitted before an agent starts.
Move any other instructions on those lines to separate lines in your
`agent-run.json`.

The reviewer command and all prompt fields are optional for compatibility with
existing configurations. When omitted, the standard reviewer command or prompt
is used. Add a `reviewer` command in `agent-run.json` to select a different
agent or model for the independent review.

The researcher writes the draft and the supervisor writes `feedback.md` with
`STATUS: REVISE` or `STATUS: READY`. A `READY` decision starts a fresh external
review. Before the reviewer starts, both old feedback files are removed; the
reviewer prompt also forbids consulting prior reviews and agent logs. The
reviewer writes `reviewer-feedback.md` with `STATUS: REJECT`,
`STATUS: REVISE`, or `STATUS: ACCEPT`. Rejection and revision both return to the
researcher, then the supervisor. The next `READY` decision starts another fresh
review. The loop ends only when the reviewer accepts the draft, including when
only negligible improvements remain. Each referee report remains available to
the researcher and supervisor during the next revision cycle. The supervisor
sees it as the reason an earlier draft was rejected or sent back. The old
`feedback.md` is removed as soon as the researcher finishes, before the next
supervisor round. If both reports are present when the researcher starts, the
supervisor's instructions take priority.

Each command may alternatively be a shell-style string, but argument arrays
are recommended because their quoting is unambiguous. Shell features such as
pipes and redirection are not interpreted.

Then run:

```sh
agent-run path/to/project
```

Agent output is shown live instead of being held until the command finishes.
While an agent is running, `agent-run` prints a heartbeat every 30 seconds with
its role, iteration, PID, and elapsed time. Iteration numbers appear only in
terminal status messages, not in agent prompts or log labels. The complete output is also appended to
`.agent-run/agent-run.log` in the project, so a failed or interrupted run can
be inspected afterward.

To change the heartbeat interval or put a time limit on each individual agent:

```sh
agent-run --heartbeat 10 --timeout 3600 path/to/project
```

To receive an email when the external reviewer accepts the paper:

```sh
agent-run . --email="you@example.com"
```

If the cluster provides `mail` or `mailx`, `agent-run` uses it automatically
when `sendmail` is unavailable. No SMTP settings are needed. You can check
delivery from the same shell where the job runs:

```sh
printf 'Cluster mail test\n' | mail -s 'agent-run test' you@example.com
```

If the cluster has no local mail command, configure an SMTP relay in the
environment:

```sh
export AGENT_RUN_SMTP_HOST="smtp.example.com"
export AGENT_RUN_EMAIL_FROM="agent@example.com"
export AGENT_RUN_SMTP_USERNAME="agent@example.com"
# Set AGENT_RUN_SMTP_PASSWORD through your cluster's secret management.
agent-run . --email="you@example.com"
```

SMTP defaults to STARTTLS on port 587. Set `AGENT_RUN_SMTP_SECURITY=ssl` for
implicit TLS (default port 465), or `AGENT_RUN_SMTP_SECURITY=none` for a trusted
relay without authentication (default port 25). `AGENT_RUN_SMTP_PORT` overrides
the default port. Username and password are optional together; authentication
requires TLS. If `AGENT_RUN_SMTP_HOST` is unset, `agent-run` uses `sendmail`,
`mail`, or `mailx`. The message includes the project and PDF paths. If sending
fails, `agent-run` reports an error after the loop finishes. The cluster must
allow connections to the chosen SMTP relay.

For an internal cluster relay that does not require a login, set the host,
sender address, and `AGENT_RUN_SMTP_SECURITY=none`; leave the username and
password unset. Ask the cluster administrator for the relay host and port.

There is no timeout by default because a legitimate research or compilation
step may take a long time. Pressing Ctrl-C stops the current agent and its
child processes cleanly; the log remains available for diagnosis.

`agent-run` exits without changing anything unless the directory contains
`paper.pdf` or `main.pdf` anywhere below it, or a top-level `TASK.md`. When
both PDF names exist, `paper.pdf` takes precedence regardless of depth;
otherwise `main.pdf` is used throughout the researcher, supervisor, and reviewer
workflow. If multiple PDFs have the preferred name, the shallowest path wins,
with alphabetical path order breaking a tie. The relative path is included in
both agent prompts. A project containing only `TASK.md` creates `paper.pdf` at
the project root by default.

After each successful researcher run, `agent-run` validates the PDF and commits
project changes with a generic Git message. It skips the commit
when there are no changes, and excludes `feedback.md`, `reviewer-feedback.md`,
`agent-run.json`, and `.agent-run` from its commits. The former commit
instruction is ignored when loading an older generated `agent-run.json`.

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
