# Product design: interactive CLI, resumable sessions, and zero-setup workspaces

## Summary

The next release makes `coding-agent` useful from the root of an ordinary local
project without requiring the user to remember a subcommand or prepare Git
manually.

It provides eight connected capabilities:

1. running bare `coding-agent` opens an interactive terminal session;
2. starting a task in a directory that is not yet a Git repository safely
   creates the repository, a protective `.gitignore`, and an initial commit;
3. typing `/` opens a keyboard-selectable session-command catalog;
4. progress lines identify the target and observable result of each tool;
5. risky operations use a concise approval summary with full redacted details
   available on demand;
6. conversational and read-only questions return a direct `ANSWERED` result
   without requiring a fake workspace change;
7. completed conversation turns persist across terminal processes and can be
   resumed by a short reference or full session ID;
8. older context is compacted automatically before it exceeds the configured
   prompt budget.

The existing `coding-agent run`, `inspect-run`, and `eval` commands remain
available for scripts and repeatable evaluation.

## Problem statements

### Interactive terminal

The current one-shot command is useful for automation but creates unnecessary
friction during exploratory development. A user must repeatedly re-enter the
workspace, model, and task command, and there is no persistent place to submit
follow-up requests or inspect the runs created in the current session.

### Git setup

The controller needs a Git baseline for change detection and an inspectable
final diff. Today, a non-Git directory is rejected with a configuration error.
That is safe but interrupts the first-run experience and asks the user to know
which initialization steps preserve secrets and produce a useful baseline.

### Interaction clarity

Raw tool names do not explain which path or command is being handled, and a
full JSON command dumped into an approval prompt is too large to evaluate.
Likewise, commands hidden behind `/help` are not discoverable before a new user
already knows that `/help` exists. The terminal needs progressive disclosure:
short target-aware progress, a summary-first approval, and an immediate command
catalog.

### Conversation continuity

An interactive process currently owns its history in memory. Exiting loses the
assistant's outcomes, so a later process cannot continue the same conversation.
Long sessions also need a controller-owned context policy; simply replaying an
ever-growing transcript would eventually exceed the model context and could
accidentally carry old approvals or verification evidence into a new run.

## Product goals

- `coding-agent` with no arguments opens a terminal UI in the current directory.
- A user can submit multiple natural-language requests without restarting the
  process.
- A user can ask an informational question and receive a direct answer without
  weakening verified completion for coding tasks.
- Typing `/` exposes all session commands immediately, with arrow-key selection
  and type-to-filter behavior.
- Model/tool progress names the directory, file, command, or output being
  handled and reports a compact observable result.
- Execution approval starts with a bounded risk summary and makes full
  redacted arguments available on demand.
- Each submitted request remains one bounded, independently logged agent run.
- The session keeps compact context for follow-up requests without weakening
  controller-owned budgets, permissions, or completion checks.
- A user can list saved sessions and resume one from its original workspace.
- Completed user requests and assistant outcomes survive normal process exit.
- Context compaction is automatic, observable, bounded, and does not require a
  second model call.
- Persisted history is untrusted context only; it never restores approvals,
  verification records, tool state, or an interrupted controller run.
- A non-Git workspace becomes a usable repository automatically before the
  first task starts.
- Existing files are captured in an initial baseline commit, except files
  protected by the generated `.gitignore`.
- Existing repositories and existing `.gitignore` files are not destructively
  rewritten.
- One-shot and evaluation workflows remain backwards compatible.

## Non-goals

- A full-screen editor, mouse UI, or terminal multiplexer.
- Token-by-token model streaming; the current event stream remains the progress
  surface.
- Parallel tasks or multiple agents in one interactive session.
- Resuming an `AgentEngine` run that was interrupted mid-tool or mid-model call.
- Sharing one session concurrently across multiple interactive processes.
- Automatically choosing or inventing a Git author identity.
- Initializing a nested repository when the selected directory is already
  inside a different Git repository.
- Replacing OS-level isolation; approved commands still run as the current user.

## User experience

### First run in an ordinary project directory

After a one-time installation, the user enters a project and runs:

```powershell
coding-agent
```

The CLI validates model configuration and Git availability before mutating the
directory. If the directory is not a repository, it reports each completed
setup action:

```text
[SETUP] Initialized Git repository.
[SETUP] Added protective and project-aware .gitignore rules.
[SETUP] Created initial commit 1a2b3c4.
```

It then opens the session:

```text
Bounded Coding Agent
Workspace: C:\work\calculator
Model: deepseek-v4-flash
Session: 7e02e19a9ec44ad5b1e52c5f49f3ed3a (new)
Type / to browse commands, or /exit to quit.

coding-agent> Fix the failing parser tests
```

Model and tool events remain visible while the task runs. When the controller
reaches a terminal state, the UI prints the status and run ID, then returns to
the prompt.

For a request such as `Who are you?`, the model calls `respond` and the UI
prints `[ANSWER] ...` followed by an `ANSWERED` session status. Repository
questions may use read-only tools first. If the current run has mutated the
workspace, the controller rejects `respond` and still requires verified
`finish` or an explicit blocked result.

### Readable progress

Each model decision and tool result names its target instead of repeating only
the tool name:

```text
[MODEL] list_directory · path=.
[TOOL] list_directory · path=. · 1 entry -> COMPLETED (2 ms)
[MODEL] create_file · path=index.html · 12640 chars
[TOOL] create_file · path=index.html · changed -> COMPLETED (18 ms)
[MODEL] run_command · program=node · cwd=. · purpose=verify · 2 args · inline code=3012 chars
[TOOL] run_command · program=node · exit=0 -> COMPLETED (412 ms)
```

File contents, inline scripts, search terms, and output bodies are represented
by bounded metadata rather than echoed into the progress stream. Consecutive
identical actions show a repetition count.

### Layered approval

An execution approval initially shows the action, bounded request summary,
OS-account risk, and the digest of the exact operation. The choice prompt is:

```text
Approve this exact digest? [y]es / [d]etails / [N]o:
```

`d` prints the complete redacted arguments as wrapped JSON and the full digest,
then asks again. This keeps the default prompt scannable without hiding the
information needed for a deliberate decision. Empty input remains a denial.

### Existing Git repository

An existing repository opens immediately. Startup does not modify its
`.gitignore`, index, commits, branch, or working tree.

### Scripted one-shot use

The existing form remains supported:

```powershell
coding-agent run --workspace C:\work\calculator "Fix the failing tests"
```

It uses the same automatic workspace setup when the selected directory is not a
repository, then exits with the existing status-specific exit code.

### Resume a conversation

The banner and `/session` expose a short session reference. From the same
repository, a later terminal can continue it using any unique hexadecimal
prefix of at least two characters:

```powershell
coding-agent resume 7e
```

`coding-agent sessions` lists recent sessions with a collision-safe short
reference, local time, turn count, and the latest user request; `--workspace`
filters the list. Inside the TUI, `/resume` opens an arrow-key selector showing
the same identifying information, while `/resume <prefix>` switches directly.
An ambiguous prefix is rejected and shows longer candidate references. A resume
request is rejected if the supplied workspace is not the canonical workspace
recorded when the session was created. This prevents history from one repository
being injected into another.

### Session commands

The initial terminal UI supports:

| Command | Behavior |
|---|---|
| `/help` | Show commands and interaction rules. |
| `/workspace` | Show the canonical workspace path. |
| `/session` | Show the resumable short reference and full session ID. |
| `/resume [prefix]` | Choose or directly resume another session in this workspace. |
| `/history` | Show persisted recent tasks, statuses, and run IDs. |
| `/clear` | Clear the terminal when ANSI control is available. |
| `/exit`, `/quit` | End the session successfully. |

An empty line is ignored. End-of-file exits successfully. `Ctrl+C` at the prompt
cancels the current input and keeps the session open; a second immediate exit
command remains explicit and predictable.

On a real terminal, `/` opens the command candidates before Enter is pressed.
Up/down arrows move a reverse-video highlight. The first Enter completes the
highlighted command into the editable prompt without submitting it; the user
may continue editing, and a later Enter submits the line. Ordinary characters
filter by command prefix, Backspace edits the filter or cancels an empty menu,
and Escape cancels. Redirected stdin retains the deterministic line-mode
behavior used by scripts.

## Conversation model

Each user submission creates a new `AgentEngine` run with its own run ID,
budgets, events, artifacts, verification evidence, and terminal result. This
preserves auditability and prevents an indefinitely growing controller state.

`ANSWERED` means the objective was satisfied without a workspace mutation. It
is not interchangeable with `SUCCEEDED`, which remains reserved for changed
code backed by fresh verification and an inspectable diff.

For follow-ups, a per-user session log stores redacted completed user requests,
assistant outcomes, statuses, run IDs, and changed-path names. The full log is
retained for audit, while the next request receives only a bounded derived view.
When the view crosses its target size, the controller deterministically folds
the oldest raw turns into structured memory and keeps the newest turns intact.
No model-generated summary is trusted or called solely for compaction.

The UI reports when compaction happens. Repository contents remain the
authoritative state, and restored history is explicitly labeled as untrusted.
It cannot grant permissions, approve an operation, restore verification
evidence, waive current budgets, or count as success for the new run.

## Automatic repository setup

### Generated ignore policy

The generated rules protect likely credentials first, then add common local,
editor, operating-system, and detected language build artifacts. At minimum the
rules cover:

- `.env` variants while allowing `.env.example`;
- private-key and common credential filenames already denied by `PathPolicy`;
- local virtual environments, caches, IDE state, and agent-local state;
- Python, Node.js, or C/C++ build artifacts when matching project markers exist.

If `.gitignore` already exists in the non-Git directory, its content is
preserved and only missing coding-agent rules are appended in a labeled block.

### Initial commit

The setup stages all non-ignored files and creates:

```text
chore: initialize repository
```

The commit uses the user's existing Git identity. If Git cannot resolve an
identity, setup stops with commands explaining how to configure it; the tool
does not create a misleading synthetic author.

### Safety exceptions

Startup stops without initializing when:

- the target does not exist or is not a directory;
- Git is not available;
- the target is a subdirectory of an enclosing repository;
- a `.git` entry exists but is not a usable repository;
- Git identity preflight fails;
- initialization, staging, or committing returns a non-zero status.

Errors report the failed setup phase without exposing model credentials. Git
subprocesses receive a filtered environment and never run through a shell.

## Success criteria

- Bare `coding-agent` reaches a prompt in an existing repository.
- `/` immediately displays every command; arrows visibly highlight a choice,
  and Enter completes it into the prompt without executing it.
- A natural-language line launches a real bounded run and returns to the prompt.
- A pure conversation ends as `ANSWERED` without a file change or rejected
  `finish` loop; `respond` after a mutation is rejected.
- Every file/directory tool progress line names its target; command progress
  names the executable, purpose, and exit code without dumping inline code.
- Approval initially stays bounded, `d` reveals full redacted arguments, and
  Enter without an affirmative answer denies the operation.
- `/history` shows the resulting run ID and terminal status.
- A completed turn can be resumed from a second CLI process by session reference.
- A unique short prefix resumes from CLI or TUI; ambiguous prefixes fail safely.
- Session discovery shows local time and the latest user request.
- Resume from a different canonical workspace is rejected before agent startup.
- Context remains under the configured hard limit and older turns compact
  automatically while recent turns remain readable.
- Persisted JSONL is redacted and contains no approval or verification IDs.
- Exiting the terminal returns code `0` without changing repository state.
- A non-Git directory with source files becomes a Git repository with one
  initial commit and a clean working tree.
- Existing `.gitignore` content is preserved.
- `.env`, private keys, and generated artifacts are absent from the initial
  commit.
- Re-running setup against an existing repository is a no-op.
- Existing `run`, `inspect-run`, and `eval` tests continue to pass.
