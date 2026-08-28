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
- A target workspace that is already a Git repository

## Install

```bash
python -m venv .venv
python -m pip install -e .
```

No runtime dependency other than Python's standard library is installed.

## Configure

Set the API key and model through environment variables. The key is used only
by the controller and is removed from the environment inherited by tool
processes.

PowerShell:

```powershell
$env:OPENAI_API_KEY = "..."
$env:CODING_AGENT_MODEL = "..."
# Optional for a compatible gateway:
$env:CODING_AGENT_BASE_URL = "https://example.invalid/v1"
```

Bash:

```bash
export OPENAI_API_KEY="..."
export CODING_AGENT_MODEL="..."
```

## Run

```bash
coding-agent run \
  --workspace path/to/target-repository \
  "Fix the parser bug and run the relevant tests"
```

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

Useful options:

```text
--approval-mode prompt|deny
--max-turns N
--max-seconds N
--command-timeout N
--json
```

Run logs and redacted output artifacts are stored under
`CODING_AGENT_RUNS_DIR`, or under the default per-user run directory. The CLI
prints a neutral run ID rather than an absolute local path.

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
