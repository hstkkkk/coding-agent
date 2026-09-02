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
Informational requests use a separate `respond` action and end as `ANSWERED`
without pretending that a code change was verified.

## Requirements

- Python 3.11 or newer
- Git on `PATH`
- An OpenAI-compatible `/chat/completions` endpoint with native tool calling
- A local target workspace directory
- Microsoft Edge, Google Chrome, or Chromium for visual Web verification

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
  "allow_programs": ["python"],
  "sessions_dir": "sessions",
  "session_context_chars": 12000
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
verification evidence. Completed requests and assistant outcomes persist in a
redacted per-user session log. A bounded view supplies continuity to follow-up
tasks without carrying approval or verification state between runs.

In an interactive terminal, typing `/` immediately opens the command list.
Use the up/down arrows to move the reverse-video highlight, then press Enter to
complete that command into the editable prompt. Continue editing if needed and
press Enter again to submit. Typing while the menu is open filters the list.
Redirected input keeps the plain line-oriented fallback. Ordinary typing and
pasted Unicode text are echoed incrementally, so long wrapped requests are not
reprinted for every character.

Session commands:

```text
/help       show commands
/workspace  show the active repository root
/session    show the short reference and full session ID
/resume     choose and resume another session in this workspace
/history    show persisted recent tasks, statuses, and run IDs
/clear      clear an ANSI-capable terminal
/exit       end the session
```

The prompt also accepts ordinary questions. A conversation that does not need
the repository answers directly; a read-only repository question may inspect
files first. Both end as `ANSWERED` and do not require a synthetic file change:

```text
coding-agent> Who are you?
[MODEL] respond
[ANSWER] I am a bounded coding agent for this local workspace.
[SESSION] ANSWERED | Run ID: ...
```

Once a run changes the workspace, `respond` is rejected and the run must use
verified `finish` or explicitly report why it is blocked.

Progress lines identify the object being handled and the observable result:

```text
[MODEL] read_file · path=index.html
[TOOL] read_file · path=index.html · lines=1-240 · 9821 chars -> COMPLETED (exec 2 ms)
[MODEL] run_command · program=node · cwd=. · purpose=verify · 2 args · inline code=2140 chars
[MODEL] browser_check · path=index.html · viewport=1280x720 · wait=500 ms
```

For approved operations, progress reports actual execution separately from the
time spent waiting for the human, for example `(exec 125 ms, approval 4000
ms)`. An identical consecutive read-only request is served by the existing
observation once, then stopped with the dedicated `STAGNATION` error if the
model ignores the corrective feedback. Exact protocol-rejection reasons are
shown in bounded, redacted warnings rather than a generic failure line.

Use the explicit subcommand when passing startup options:

```powershell
coding-agent tui --allow-program python --max-turns 40
```

The banner prints a short session reference. After exiting, resume from the
same repository using any unique hexadecimal prefix of at least two characters:

```powershell
coding-agent resume 93
```

`coding-agent sessions`, optionally filtered with `--workspace`, shows each
unique short reference, local activity time, turn count, and last user request.
Inside the TUI, `/resume` opens an arrow-key selector with the same information;
`/resume 93` switches directly. Ambiguous prefixes are rejected with candidate
references. Sessions with no completed turn are omitted from discovery and a
new empty session is discarded on exit or after switching to an older session,
so simply opening the TUI does not create resume-list clutter. Resume still
rejects a different canonical workspace. When older
history crosses the configured target, the controller automatically compacts it
into bounded structured memory and reports the event. The append-only JSONL
remains available for audit, but restored text is untrusted context only and
never restores a partially completed run, permissions, approvals, or
verification evidence.

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

By default, command execution, browser rendering, whole-file replacement, and
deletion require an exact interactive approval. Read-only tools and narrow hash-guarded edits run
automatically. Before editing, replacing, or deleting an existing UTF-8 file, the
controller stores a complete redacted before-image. If an exact recovery copy
cannot be stored, the operation is rejected. The approval warns explicitly
when a pre-existing untracked file cannot be restored by Git.
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
--session-context-chars N  interactive commands only; 2000..18000
--json                    one-shot run only
```

Run logs and redacted output artifacts are stored under `runs_dir` from the
user settings file, `CODING_AGENT_RUNS_DIR`, or the default per-user run
directory, in that order. The CLI prints a neutral run ID rather than an
absolute local path.

Interactive session logs use `sessions_dir` from user settings,
`CODING_AGENT_SESSIONS_DIR`, or `~/.coding-agent/sessions`, in that order.

## Inspect a run

```bash
coding-agent inspect-run <run-id>
coding-agent inspect-run <run-id> --json
```

Run inspection prints recovery IDs beside existing-file mutations. Restore
one to a new path (existing destinations are never overwritten) with:

```powershell
coding-agent recover-file <run-id> <recovery-id> --output recovered-file.txt
```

The final completion gate reads the complete Git diff, including the contents
of new untracked UTF-8 files. An empty or internally truncated diff cannot be
used to claim success.

For Web/UI/animation objectives, a syntax check is not sufficient evidence.
The controller requires a fresh cited `browser_check`, which loads a local HTML
entry point in Edge, Chrome, or Chromium with a disposable profile, captures a
PNG, and stores the rendered DOM. Export the screenshot without overwriting an
existing file:

```powershell
coding-agent export-screenshot <run-id> <screenshot-id> --output preview.png
```

The browser check proves that the current page rendered at the requested
viewport; it does not claim that subjective appearance is correct. Browser
scripts still execute with the current OS account and therefore require human
approval.

## Test

The default suite is deterministic, offline, and requires no API key:

```bash
python -m unittest discover -s tests -v
```

It covers model protocol validation, completion gating, stale and browser
verification,
non-mutating answers, retry limits, path escape attempts, secret-file policy,
edit conflicts, approval, subprocess behavior, output redaction, and a full
real-tool loop in a temporary Git repository.

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
