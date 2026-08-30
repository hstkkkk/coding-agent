# Architecture

## System contract

Given a natural-language task and one local workspace, the trusted CLI prepares
a Git baseline when necessary. The agent may then inspect files, make bounded
edits, run approved commands, and iterate on real tool results. It returns
either verified changes or an explicit blocked, failed, or cancelled status.

The first release targets small bug fixes and small feature additions. It does
not target multi-repository work, deployment, GUI automation, long-running
tasks, or multi-agent coordination.

## Deep modules and seams

`AgentEngine.run(TaskRequest) -> RunResult` is the main external interface. Its
implementation owns the loop, state transitions, retry budget, context view,
stagnation detection, verification freshness, and terminal state.

`LocalAgentRunner.run(objective) -> RunResult` is the local application
interface used by both one-shot and interactive commands. Its implementation
creates a new run ID, events, artifacts, tools, and `AgentEngine` for every
call, so interactive turns never share controller state or verification.

`InteractiveSession.run(run_task) -> int` owns the terminal grammar, slash
commands, presentation, and bounded history. It supplies recent outcomes only
as labeled context; the repository remains authoritative and every request
crosses the same `LocalAgentRunner` interface.

Internal seams have at least production and test adapters:

| Seam | Production adapter | Test adapter |
|---|---|---|
| Model | `OpenAICompatibleAdapter` | `ScriptedModelAdapter` |
| Tools | `LocalToolRuntime` | scenario fake |
| Approval | terminal prompt/scoped policy | fixed decision |
| Events | JSONL/console | in-memory sink |
| Clock | system clock | virtual clock |
| Terminal | stdio with optional ANSI styling | injected text streams |

Vendor responses, subprocess objects, and CLI rendering do not enter the core
data model.

## Workspace startup

Before constructing `AgentEngine`, the CLI validates model configuration and
calls `prepare_workspace(path)`. Existing Git roots are strict no-ops. A
non-Git directory is initialized with credential-protecting and project-aware
ignore rules, then all non-ignored files are captured in an initial commit.
Directories nested inside an enclosing repository are rejected instead of
silently expanding the workspace or creating a nested repository.

This startup path is trusted, human-requested orchestration. It uses bounded,
shell-free Git subprocesses with a filtered environment. It is not exposed as a
model tool; Git operations inside the Agent Loop remain read-only.

## Controller loop

```text
INITIALIZING
  -> REQUEST_MODEL
  -> VALIDATE_ACTION
  -> EXECUTE_TOOL
  -> RECORD_OBSERVATION
  -> REQUEST_MODEL
  -> VERIFY_FINISH
  -> SUCCEEDED | BLOCKED | FAILED | CANCELLED
```

Every model turn must normalize to exactly one of:

```text
ToolCall | FinishRequest | BlockedRequest
```

Multiple tool calls, unknown tools, or malformed arguments are protocol errors.
The controller permits a bounded correction attempt without executing a
partial action.

## Tool execution

The model sees eleven local tools:

```text
list_directory  read_file      search_text
edit_file       create_file    delete_file
run_command     git_status     git_diff
read_output     search_output
```

Filesystem paths are workspace-relative. The implementation resolves traversal,
symbolic links, and junctions before checking containment. Direct `.git` and
likely credential-file access is rejected. Edits require both an expected file
hash and exactly one matching old-text fragment; writes use a temporary file
and atomic replacement.

Commands use a program-plus-argument array with `shell=False`. They receive a
small environment allowlist that excludes the model API key. Command execution
and deletion require approval unless a narrow startup policy pre-authorizes the
executable. Output is bounded, redacted, stored as an artifact, and exposed to
the model through previews and paged reads.

## State and context

The event log and `RunState` are authoritative. The prompt is a bounded,
derived view containing:

- the original objective and invariant rules;
- workspace version and changed paths;
- recent verification records and errors;
- recent tool actions and normalized observations;
- the current tool schemas.

Old events remain on disk when they fall out of the model view. Repository
contents and tool output are treated as untrusted data and cannot grant new
permissions.

## Verification freshness

Every detected workspace mutation increments `workspace_version`. A
`VerificationRecord` stores its command, exit code, output artifact, result, and
workspace version. A later source mutation makes the older evidence stale.

The model's `finish` request is accepted only when:

1. at least one workspace change was recorded;
2. every cited verification ID exists;
3. at least one cited verification passed on the current version;
4. the controller can inspect the final Git diff.

The controller proves that the evidence ran against the current state; it does
not claim that a finite test suite proves semantic correctness.

## Error and retry policy

Temporary model transport failures use bounded exponential backoff.
Authentication and malformed requests do not retry. Model protocol mistakes
receive structured feedback up to a small limit. File conflicts, command
failures, timeouts, approval denials, and policy denials become observations;
non-idempotent local actions are never retried automatically.

Three identical consecutive actions trigger stagnation. Model turns, wall time,
command time, output volume, and protocol mistakes all have controller-owned
limits.

## Evaluation

Evaluation does not trust `RunResult` alone. Each task starts from a fresh Git
fixture. After the agent stops, hidden files are injected and an independent
Oracle command runs outside the Agent Loop. A claimed success with a failing
Oracle is counted explicitly as a false success.

## Known limitations

- no operating-system or container sandbox;
- no checkpoint/resume after a crash;
- no automatic network isolation for approved subprocesses;
- no parallel tools or multi-agent coordination;
- line-oriented terminal UI rather than a full-screen editor;
- interactive history is in-memory and is not resumed after process exit;
- no token-by-token model streaming;
- UTF-8 text editing only;
- optimized for small repositories and bounded tasks;
- heuristic secret detection can have false negatives;
- one OpenAI-compatible chat-completions adapter in the first release.

