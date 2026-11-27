# Agent Runner

`agent-run` runs a researcher, supervisor, and independent paper reviewer in a
project directory. Start a new investigation by writing the question in a
top-level `TASK.md` (or `task.md`). Use `--paper` to work directly on a
manuscript, starting from a brief or an existing `paper.pdf` or `main.pdf`.

## Requirements

- Python 3.10 or newer
- Git
- The `claude` and `codex` command-line tools, installed and authenticated

## Install from GitHub

Install the default branch with
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

| Command | Starting point | First turn |
| --- | --- | --- |
| `agent-run .` | A task brief, optionally with a manuscript | Investigate the question |
| `agent-run . --paper=true` | A manuscript or a task brief | Write or revise the paper |

`--paper` is shorthand for `--paper=true`. Both workflows finish with independent review.

On every turn, the runner instructs the researcher, supervisor, and independent
reviewer to conduct a thorough literature search. This applies to both modes,
including revisions, resumed turns, and projects with custom prompts. Each agent
compares the question, methods, and results with the closest primary sources,
cites what it verified in its assigned output, and identifies what the project
adds. Agents report limits on access or coverage and qualify novelty claims.

For example, a project can start with a single `TASK.md` containing:

> Does the median give a more stable estimate of delivery time than the mean
> for the routes in deliveries.csv? Check whether the answer depends on route size.

Include any data, constraints, or criteria that matter to your question. A
theoretical question or literature synthesis can begin with the question alone.
Run `agent-run path/to/project`. A brief starts a new run in the investigation
phase even when the directory also contains a manuscript. If both spellings
exist, `TASK.md` takes precedence.

| Stage | Work and handoff | Decision |
| --- | --- | --- |
| Investigation | The researcher reads the brief, carries out research, and records methods, findings, limitations, and links to supporting evidence in `research.md`. The supervisor checks that evidence against the question and writes `feedback.md`. | `CONTINUE` requests specific follow-up. `WRITE` approves the evidence for drafting. A well-supported negative result can qualify. |
| Manuscript | The researcher writes or revises the editable paper and generates the PDF. The supervisor checks the draft against the evidence. | `REVISE` returns to drafting. In research mode, `RESEARCH` reopens the investigation. `READY` sends the paper for independent review. |
| Independent review | The referee reads the draft, can inspect the evidence, and writes `reviewer-feedback.md`. | `REVISE` and `REJECT` return to the author and supervisor. `ACCEPT` completes the run. |

Each decision is the final line of the corresponding report, for example
`STATUS: WRITE`. Each turn launches the configured command in a new process.
The default commands start fresh conversations. The brief, research record,
artifacts, draft, and current feedback carry context across turns.

After an interruption, rerun the same command. The runner saves the pending
mode, round, pending turn and selected PDF path in `.agent-run/workflow.json`.
Resume with the same mode. An interrupted turn
runs again and can use any work already on disk. Acceptance clears the
checkpoint, so a later invocation starts a new run.

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

- All prompts: `{{pdf_path}}`
- Researcher and supervisor in both phases: `{{task_instruction}}`
- Researcher only: `{{feedback_instruction}}`
- Supervisor only: `{{reviewer_feedback_instruction}}`

The optional `investigation_prompt` and `investigation_supervisor_prompt`
configure the research turns. Older project configurations automatically use
the packaged investigation prompts and keep their existing writing prompts.

Set `"journal"` to a title such as `"Physical Review Letters"` to have the
writing and review agents consult its official guidance. With an empty or
omitted field, research mode uses the brief's audience and disciplinary
conventions. Paper mode retains the Physical Review family default.
Journal instructions start at the manuscript stage.

Agents are not told the current iteration or the 100-iteration safety limit.
For compatibility with older project configurations, prompt lines containing
the former `{{iteration}}` placeholder are omitted before an agent starts.
Move any other instructions on those lines to separate lines in your
`agent-run.json`.

The reviewer command and all prompt fields are optional for compatibility with
existing configurations. When omitted, the standard reviewer command or prompt
is used. Add a `reviewer` command in `agent-run.json` to select a different
agent or model for the independent review.

Before independent review, the runner removes both previous feedback files.
The default referee prompt also forbids consulting prior reviews and agent
logs. Each new referee report remains available to the researcher and
supervisor through revision, including any reopened investigation. The runner
removes the previous `feedback.md` before the supervisor starts. If both
reports are present when the researcher starts, supervisor instructions take
priority.

Each command may alternatively be a shell-style string, but argument arrays
are recommended because their quoting is unambiguous. Shell features such as
pipes and redirection are not interpreted.

Then run:

```sh
agent-run path/to/project
```

By default, the terminal shows agent start, heartbeat, and completion messages
without printing every file read, tool result, or generated diff. If an agent
fails, the terminal shows a short excerpt of its recent error output. The full
stdout and stderr are always appended to `.agent-run/agent-run.log` in the
project. Use `--verbose` to stream the full agent output to the terminal as
well.

While an agent is running, `agent-run` prints a heartbeat every 30 seconds with
its role, iteration, PID, and elapsed time. Iteration numbers appear only in
terminal status messages, not in agent prompts or log labels. The log remains
available after a failed or interrupted run.

To change the heartbeat interval or put a time limit on each individual agent:

```sh
agent-run --heartbeat 10 --timeout 3600 path/to/project
```

## Progress dashboard

Open `.agent-run/dashboard.html` in your browser during a run. This standalone
file reloads every 15 seconds. It shows the mode, phase, agent, round, last
update, and the last 20 events from the current invocation. Long-running turns
refresh the last-update timestamp on each runner heartbeat. A hard shutdown
can leave the last recorded state on screen, so check its timestamp.

### Monitor a remote run on GitHub Pages

1. Copy `src/agent_runner/index.html` to the publishing folder of your chosen
   GitHub Pages repository, for example as `agent-monitor/index.html` on your
   personal site. Publish that file once. GitHub Pages supports free hosting
   from public repositories. [GitHub Pages setup](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)
2. In your browser, [create a Gist](https://gist.github.com/) with a file named
   `status.json` containing `{}`. Copy the ID at the end of its URL. Use a
   separate Gist for each concurrent job.
3. On a cluster without `gh`, create a fine-grained personal access token for
   the Gist's owner with **Account permissions → Gists → Read and write**.
   Provide it to the runner job as `AGENT_RUN_GIST_TOKEN` through your job's
   secret environment. For an interactive Bash session, enter it at a hidden
   prompt with the commands below. The runner updates the Gist over HTTPS
   using Python's standard library. If you already use an authenticated
   GitHub CLI, the runner can use `gh` when the token variable is unset.
   [GitHub Gist permissions](https://docs.github.com/en/rest/gists/gists#update-a-gist)

   ```bash
   read -rsp 'GitHub Gist token: ' AGENT_RUN_GIST_TOKEN
   echo
   export AGENT_RUN_GIST_TOKEN
   ```
4. Start the runner with `agent-run . --status-gist GIST_ID`, adding `--paper`
   or `--email=you@example.com` as desired. Supply the same ID when resuming.
5. Open the hosted dashboard and enter the Gist ID, or bookmark the page with
   `?gist=GIST_ID`. The browser reads the feed every two minutes. The runner
   updates it at turn boundaries and about every five minutes during long turns.

The full dashboard and the compact widget identify each project by its folder
name. The runner includes the folder's basename in each status update.

The shared feed contains mode, phase, role, round, status, and timestamps.
Anyone with the Gist link can read those fields. A failed Gist update prints
a warning and the local dashboard continues recording the run.

## Email notifications

To receive an email when the external reviewer accepts the paper or the running
loop stops with an error:

```sh
agent-run . --email="you@example.com"
```

Each subject identifies research or paper mode and whether the job completed
or stopped. The email includes the local dashboard path. A failure email includes
the error, project path, PDF path, and agent log path. Interrupting the loop
with Ctrl-C also sends a failure email. The runner checks mail delivery before
starting the loop; it cannot notify if the process or host is forcibly killed
or mail delivery itself is unavailable.

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

If an agent exits because its selected model is temporarily at capacity,
`agent-run` retries that agent up to three times after 15, 30, and 60 seconds.
Partial supervisor or reviewer reports are removed before a retry. Other
failures still stop the run immediately; after the final capacity failure the
runner reports the error and keeps the project files for a later rerun.

`agent-run` reports a startup error if the project root has no `TASK.md` or
`task.md`. In paper mode, a project can also start from an existing
`paper.pdf` or `main.pdf` anywhere in its directory tree. When
both PDF names exist, `paper.pdf` takes precedence regardless of depth;
otherwise `main.pdf` is used throughout the researcher, supervisor, and reviewer
workflow. If multiple PDFs have the preferred name, the shallowest path wins,
with alphabetical path order breaking a tie. The relative path is included in
the manuscript prompts. A project containing only a brief creates `paper.pdf`
at the project root during the writing phase.

After an investigation turn, `agent-run` checks for a nonempty `research.md`.
After a writing turn, it validates the PDF. It commits the researcher's project
changes after either check. It skips the commit
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
