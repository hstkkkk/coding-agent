# Bounded Coding Agent

A small, controller-driven coding agent for one local Git repository at a time.
The model may propose exactly one structured action per turn; a local controller
validates and executes the action, records the observation, and decides whether
the run may stop.

This is an original implementation of the agent loop. It uses no agent SDK or
hosted file/command tool. Runtime code depends only on the Python standard
library.

## Why this design

```text
User / CLI
    |
    v
AgentEngine <------> OpenAI-compatible model adapter
    |
    v
Policy + LocalToolRuntime
    |
    +--> workspace-confined file tools
    +--> approved subprocess execution
    +--> read-only Git observations
    |
    v
ToolResult + verification evidence + JSONL events
```

The language model is an untrusted decision module, not the control plane.
Permissions, budgets, retries, workspace versions, and terminal states remain
controller-owned. `finish` is only a request: completion is accepted only when
the cited successful verification belongs to the current workspace version.

## Requirements

- Python 3.11 or newer
- Git on `PATH`
- An OpenAI-compatible `/chat/completions` endpoint with native tool calling
- A local target workspace directory

## Install

For development in this checkout:

```bash
python -m venv .venv
python -m pip install -e .
```

To make `coding-agent` available from arbitrary target directories with `uv`,
install the console command once:

```powershell
uv tool install --editable "C:/path/to/coding-agent"
uv tool update-shell
```

Open a new terminal if `uv` reports that it changed `PATH`.

No runtime dependency other than Python's standard library is installed.

## Configure

Create the fixed per-user configuration file:

```text
C:\Users\<you>\.coding-agent\settings.json
```

The cross-platform path is `~/.coding-agent/settings.json`. A typical
DeepSeek-compatible configuration is:

```json
{
  "api_key": "<your-api-key>",
  "model": "deepseek-v4-flash",
  "base_url": "https://api.deepseek.com",
  "thinking": "disabled",
  "max_turns": 30,
  "max_seconds": 900,
  "command_timeout": 120,
  "approval_mode": "prompt",
  "allow_programs": ["python"]
}
```

The file must be a UTF-8 JSON object and is strictly validated. Keep it outside
repositories, do not commit it, and restrict access to your user account
because `api_key` is stored as plain text. The key is used only by the
controller, redacted from saved output, and omitted from tool-process
environments.

Configuration precedence is:

```text
explicit command-line option > user settings file > environment variable > built-in default
```

An explicit `api_key` in the file is preferred over an environment key. To
keep the secret in an environment variable instead, omit `api_key` and set
`api_key_env` to its variable name; the default name is `OPENAI_API_KEY`.

Environment variables remain supported as a fallback:

PowerShell:

```powershell
$env:OPENAI_API_KEY = "..."
$env:CODING_AGENT_MODEL = "..."
# Optional for a compatible gateway:
$env:CODING_AGENT_BASE_URL = "https://example.invalid/v1"
# Optional when a provider defaults to a thinking mode that rejects forced tools:
$env:CODING_AGENT_THINKING = "disabled"
```

Bash:

```bash
export OPENAI_API_KEY="..."
export CODING_AGENT_MODEL="..."
export CODING_AGENT_THINKING="disabled"
```

`CODING_AGENT_THINKING` accepts `enabled` or `disabled`. Leave it unset to
preserve the provider default. The equivalent command-line option is
`--thinking enabled|disabled`. See [docs/configuration.md](docs/configuration.md)
for the complete schema, ranges, precedence rules, and security notes.

## Interactive terminal

After configuring the model environment, enter a target project and run the
bare command:

```powershell
Set-Location "C:/path/to/target-project"
coding-agent
```

This opens a line-oriented terminal session. Each natural-language submission
creates a fresh bounded run with its own ID, budgets, event log, artifacts, and
verification evidence. The working tree and a compact summary of up to six
recent requests provide continuity for follow-up tasks.

In an interactive terminal, typing `/` immediately opens the command list.
Use the up/down arrows and Enter to select, or continue typing to filter the
list. Redirected input keeps the plain line-oriented fallback.

Session commands:

```text
/help       show commands
/workspace  show the active repository root
/history    show recent tasks, statuses, and run IDs
/clear      clear an ANSI-capable terminal
/exit       end the session
```

Progress lines identify the object being handled and the observable result:

```text
[MODEL] read_file · path=index.html
[TOOL] read_file · path=index.html · lines=1-240 · 9821 chars -> COMPLETED (2 ms)
[MODEL] run_command · program=node · cwd=. · purpose=verify · 2 args · inline code=2140 chars
```

Use the explicit subcommand when passing startup options:

```powershell
coding-agent tui --allow-program python --max-turns 40
```

## One-shot run

```bash
coding-agent run \
  --workspace path/to/target-repository \
  "Fix the parser bug and run the relevant tests"
```

If the target is not yet a Git repository, the trusted CLI startup path creates
one, appends protective and project-aware `.gitignore` rules, stages the
non-ignored baseline, and creates `chore: initialize repository` with the
user's configured Git identity. Existing repositories are not modified during
startup. A subdirectory of an enclosing repository is rejected; run from that
repository's root instead.

By default, command execution and deletion require an exact interactive
approval. Read-only tools and hash-guarded workspace edits run automatically.
For a controlled demo, an executable can be pre-authorized explicitly:

```bash
coding-agent run \
  --workspace path/to/target-repository \
  --allow-program python \
  "Fix the failing unit test"
```

Pre-authorizing an executable permits the target repository to execute code
through it. Use the option only for repositories you are prepared to run.

When approval is required, the first screen shows a bounded command summary,
risk, and operation digest. Choose `d` to inspect the complete redacted JSON
arguments with visual wrapping, `y` to approve that exact digest, or `n` to
deny. Long inline scripts are never dumped into the initial approval prompt.

Useful options:

```text
--approval-mode prompt|deny
--max-turns N
--max-seconds N
--command-timeout N
--allow-program NAME
--json                    one-shot run only
```

Run logs and redacted output artifacts are stored under `runs_dir` from the
user settings file, `CODING_AGENT_RUNS_DIR`, or the default per-user run
directory, in that order. The CLI prints a neutral run ID rather than an
absolute local path.

## Inspect a run

```bash
coding-agent inspect-run <run-id>
coding-agent inspect-run <run-id> --json
```

## Test

The default suite is deterministic, offline, and requires no API key:

```bash
python -m unittest discover -s tests -v
```

It covers model protocol validation, completion gating, stale verification,
retry limits, path escape attempts, secret-file policy, edit conflicts,
approval, subprocess behavior, output redaction, and a full real-tool loop in a
temporary Git repository.

## Evaluate

The included evaluation suite copies each fixture into a fresh temporary Git
repository, records the initial Oracle failure, runs the agent, injects tests
that were outside the agent workspace, and executes the Oracle independently:

```bash
coding-agent eval --suite evaluation/suite.json
```

The report includes pass rate, false-success count, model turns, tool calls,
workspace versions, changed files, and independent Oracle results.

## Safety limits

This project provides workspace-level access controls, explicit approval,
secret filtering, timeouts, process-tree termination, and bounded outputs. It
is not an operating-system sandbox: approved repository code still runs with
the current user's permissions and may access resources that the operating
system permits. See [SECURITY.md](SECURITY.md) and
[docs/architecture.md](docs/architecture.md) for the full threat model and
design tradeoffs.
