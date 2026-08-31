# Architecture

## System contract

Given a natural-language task and one local workspace, the trusted CLI prepares
a Git baseline when necessary. The agent may then inspect files, make bounded
edits, run approved commands, and iterate on real tool results. It returns
either verified changes, a non-mutating informational answer, or an explicit
blocked, failed, or cancelled status.

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

`ConversationStore.create/resume/list` and `ConversationSession.prepare/record`
own durable conversation identity, workspace binding, JSONL validation,
redaction, and automatic context compaction. Their small interface prevents the
terminal and CLI from depending on the persistence format.

`InteractiveSession.run(run_task) -> int` owns the terminal grammar, slash
commands, and presentation. It delegates history and bounded context to one
`ConversationSession`; the repository remains authoritative and every request
crosses the same `LocalAgentRunner` interface.

`TerminalPrompt.readline(prompt) -> str` owns editable terminal input and the
slash-command selector. It hides Windows/POSIX key decoding, Unicode display
width, menu filtering, cursor movement, and the redirected line-mode fallback.

`describe_tool(...)` and `describe_tool_result(...)` project untrusted tool
arguments/results into bounded, content-free progress details shared by events
and approval summaries.

`load_user_settings(path) -> UserSettings` is the per-user configuration
boundary. It owns the fixed path, bounded UTF-8 JSON decoding, complete schema
validation, relative run/session-directory resolution, and secret-safe errors. The CLI
receives only the validated immutable value object.

Internal seams have at least production and test adapters:

| Seam | Production adapter | Test adapter |
|---|---|---|
| Model | `OpenAICompatibleAdapter` | `ScriptedModelAdapter` |
| Tools | `LocalToolRuntime` | scenario fake |
| Approval | terminal prompt/scoped policy | fixed decision |
| Events | JSONL/console | in-memory sink |
| Clock | system clock | virtual clock |
| Terminal | raw-key prompt plus line fallback | injected key reader/text streams |
| User settings | fixed per-user JSON file | temporary JSON file or value object |
| Conversation | locked per-user JSONL store | temporary-directory store |

Vendor responses, subprocess objects, and CLI rendering do not enter the core
data model.

## Configuration startup

Normal commands load `~/.coding-agent/settings.json` before workspace setup.
Explicit CLI values override file values; file values override compatible
environment variables; built-in defaults apply last. The file is optional, but
when present it must be a JSON object using only the documented fields and must
fit within 64 KiB. Help output bypasses loading so a malformed file cannot hide
the repair instructions.

The direct API key is carried only into the model adapter and redactor. It is
not rendered in configuration errors or object representations and does not
enter the model-visible context or the allowlisted subprocess environment.

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
     -> RESPOND -> ANSWERED
     -> EXECUTE_TOOL -> RECORD_OBSERVATION -> REQUEST_MODEL
     -> VERIFY_FINISH -> SUCCEEDED
     -> BLOCKED
  -> FAILED | CANCELLED
```

Every model turn must normalize to exactly one of:

```text
ToolCall | AnswerRequest | FinishRequest | BlockedRequest
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

It also sees three controller actions: `respond`, `finish`, and
`report_blocked`. `respond` terminates as `ANSWERED` for conversational or
informational objectives that require no mutation. The controller rejects it
after any recorded workspace mutation, so it cannot bypass the `finish`
verification gate.

Filesystem paths are workspace-relative. The implementation resolves traversal,
symbolic links, and junctions before checking containment. Direct `.git`,
`.coding-agent`, and likely credential-file access is rejected. Edits require
both an expected file hash and exactly one matching old-text fragment; writes
use a temporary file and atomic replacement.

Commands use a program-plus-argument array with `shell=False`. They receive a
small environment allowlist that excludes the model API key. Command execution
and deletion require approval unless a narrow startup policy pre-authorizes the
executable. Output is bounded, redacted, stored as an artifact, and exposed to
the model through previews and paged reads.

Human progress events carry a bounded tool target/outcome rather than raw
contents or command arguments. Approval shows the same concise description,
the OS-account risk, and a short digest first. The user can request full
redacted JSON arguments and the full digest before approving; only an explicit
affirmative answer approves.

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

Interactive continuity is a separate layer around independent runs. Each
completed turn appends a redacted record containing only the request, assistant
outcome, terminal status, run ID, and changed-path names. Resume validates a
32-hex ID and requires the original canonical workspace. It does not restore an
in-flight engine, approvals, tool observations, budgets, or verification
records.

Before a follow-up, the conversation store renders recent turns under a hard
character budget. When the target is exceeded, it deterministically folds the
oldest turns into persisted structured digests while retaining recent raw
turns. This automatic compaction uses no additional model call. Restored text
is labeled untrusted and cannot override the current repository or controller.

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

`ANSWERED` is a separate non-mutating terminal state. It is valid before any
workspace mutation, including after read-only inspection, and does not claim a
verified code change.

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
Oracle is counted explicitly as a false success. Only `SUCCEEDED` can pass a
coding evaluation; `ANSWERED` is always a non-pass.

## Known limitations

- no operating-system or container sandbox;
- no checkpoint/resume for an in-flight run after a crash; only completed
  conversation turns can be resumed;
- no automatic network isolation for approved subprocesses;
- no parallel tools or multi-agent coordination;
- line-oriented terminal UI rather than a full-screen editor;
- no concurrent multi-process use of one conversation session;
- no token-by-token model streaming;
- UTF-8 text editing only;
- optimized for small repositories and bounded tasks;
- heuristic secret detection can have false negatives;
- one OpenAI-compatible chat-completions adapter in the first release;
- one fixed per-user settings file with no project-local configuration layers.

