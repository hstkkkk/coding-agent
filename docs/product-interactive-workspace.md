# Product design: interactive CLI and zero-setup workspaces

## Summary

The next release makes `coding-agent` useful from the root of an ordinary local
project without requiring the user to remember a subcommand or prepare Git
manually.

It adds two connected capabilities:

1. running bare `coding-agent` opens an interactive terminal session;
2. starting a task in a directory that is not yet a Git repository safely
   creates the repository, a protective `.gitignore`, and an initial commit.

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

## Product goals

- `coding-agent` with no arguments opens a terminal UI in the current directory.
- A user can submit multiple natural-language requests without restarting the
  process.
- Each submitted request remains one bounded, independently logged agent run.
- The session keeps compact context for follow-up requests without weakening
  controller-owned budgets, permissions, or completion checks.
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
- Persistent conversation resume after the terminal process exits.
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
Type /help for commands or /exit to quit.

coding-agent> Fix the failing parser tests
```

Model and tool events remain visible while the task runs. When the controller
reaches a terminal state, the UI prints the status and run ID, then returns to
the prompt.

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

### Session commands

The initial terminal UI supports:

| Command | Behavior |
|---|---|
| `/help` | Show commands and interaction rules. |
| `/workspace` | Show the canonical workspace path. |
| `/history` | Show tasks, statuses, and run IDs from this terminal session. |
| `/clear` | Clear the terminal when ANSI control is available. |
| `/exit`, `/quit` | End the session successfully. |

An empty line is ignored. End-of-file exits successfully. `Ctrl+C` at the prompt
cancels the current input and keeps the session open; a second immediate exit
command remains explicit and predictable.

## Conversation model

Each user submission creates a new `AgentEngine` run with its own run ID,
budgets, events, artifacts, verification evidence, and terminal result. This
preserves auditability and prevents an indefinitely growing controller state.

For follow-ups, the terminal session supplies a bounded summary of recent user
requests and outcomes as context. Repository contents remain the authoritative
state, and previous terminal outcomes do not grant permissions or count as
verification evidence for the new run.

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
- A natural-language line launches a real bounded run and returns to the prompt.
- `/history` shows the resulting run ID and terminal status.
- Exiting the terminal returns code `0` without changing repository state.
- A non-Git directory with source files becomes a Git repository with one
  initial commit and a clean working tree.
- Existing `.gitignore` content is preserved.
- `.env`, private keys, and generated artifacts are absent from the initial
  commit.
- Re-running setup against an existing repository is a no-op.
- Existing `run`, `inspect-run`, and `eval` tests continue to pass.
