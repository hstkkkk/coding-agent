# Technical design: resumable interactive CLI and Git workspace bootstrap

## Design constraints

The implementation must preserve the current controller contract:

- `AgentEngine` remains the only module that decides run terminal state;
- each model turn still normalizes to exactly one action;
- `ANSWERED` remains distinct from verified `SUCCEEDED` and is allowed only
  before any recorded workspace mutation;
- verification evidence remains scoped to one run and workspace version;
- target repository content and prior output remain untrusted;
- Git operations exposed to the model remain read-only;
- runtime dependencies remain Python standard library only.

Automatic initialization is a trusted CLI startup operation requested by the
human. It is not a new model-visible tool and does not relax the runtime Git
write policy.

## Module shape

```text
CLI composition root
  |
  +--> WorkspaceBootstrap.prepare(path) -> WorkspaceSetupResult
  |
  +--> LocalAgentRunner.run(objective) -> RunResult
  |       |
  |       +--> AgentEngine + model/tools/events/artifacts
  |
  +--> ConversationStore.create/resume/list
  |       |
  |       +--> validated append-only JSONL + deterministic compaction
  |
  +--> InteractiveSession.run(run_task) -> int
          |
          +--> TerminalPrompt.readline(prompt) -> str
          +--> ConversationSession.prepare/record/history

AgentEngine event projection
  |
  +--> describe_tool(name, arguments) -> bounded detail
  +--> describe_tool_result(name, arguments, data) -> bounded outcome

LocalToolRuntime approval
  |
  +--> PromptApprovalAdapter.request(request) -> ApprovalDecision
```

These are deep modules: callers learn a small operation set while Git recovery,
per-run composition, command parsing, durable replay, context bounds, and
presentation remain local to their implementations.

## 1. `WorkspaceBootstrap`

### Interface

`src/coding_agent/workspace.py` exposes:

```python
@dataclass(frozen=True, slots=True)
class WorkspaceSetupResult:
    workspace: Path
    initialized: bool
    gitignore_updated: bool
    initial_commit: str | None
    messages: tuple[str, ...]

def prepare_workspace(path: Path) -> WorkspaceSetupResult:
    ...
```

Callers receive canonical state and user-facing, non-secret setup messages.
Failures raise `WorkspaceSetupError`, which the CLI normalizes as a
configuration error.

### Algorithm

1. Resolve the path and require an existing directory.
2. Verify that `git` is executable with a bounded, shell-free subprocess.
3. Probe `git rev-parse --show-toplevel`.
   - If the canonical top level equals the target, validate `git status` and
     return an unchanged result.
   - If an enclosing top level differs, reject the target instead of silently
     widening the workspace or creating a nested repository.
   - If a `.git` entry exists but the probe fails, report an invalid repository.
4. Before mutation, run `git var GIT_AUTHOR_IDENT` using the filtered Git
   environment. A failure reports the missing identity.
5. Run `git init -q`.
6. Merge required ignore patterns atomically.
7. Run `git add --all` and create `chore: initialize repository`.
8. Resolve `HEAD`, require a clean `git status --porcelain`, and return the
   abbreviated commit ID.

All Git invocations use argument arrays, `shell=False`, a timeout, bounded
captured output, and a filtered environment containing only OS execution,
locale, home/Git configuration, and Git identity variables. Model credentials
are excluded. No automatic retry is performed for mutating commands.

### `.gitignore` construction

The implementation owns one ordered pattern list:

1. sensitive names and suffixes aligned with `PathPolicy`;
2. OS/editor/agent-local state;
3. language-specific groups selected by project markers.

Existing content is preserved verbatim. A complete, internally de-duplicated
managed rule set is appended under `# Added by coding-agent workspace setup`.
The managed rules intentionally remain last even when an earlier pattern is
identical: a later user negation such as `!.env` must not expose credentials to
the initial commit. Writes use a same-directory temporary file and `os.replace`,
preserving existing content and newline termination.

Project detection is deterministic and filesystem-only:

| Markers | Additional ignores |
|---|---|
| `pyproject.toml`, `setup.py`, `requirements*.txt` | Python caches, virtualenvs, coverage, package output |
| `package.json` | `node_modules`, package-manager caches, coverage, distribution output |
| `CMakeLists.txt`, `*.sln`, `*.vcxproj` | CMake and compiler build output |

The initial commit stages all files not excluded by the resulting rules. The
ignore list includes every credential filename currently blocked by
`PathPolicy`, so those files remain untracked.

### Idempotency and failure state

Existing valid repositories are strict no-ops. The setup never amends or
rewrites history.

Preflight failures happen before mutation. A failure after `git init` may leave
the newly created repository and `.gitignore` in place; the error reports the
completed phase so the user can inspect it. The implementation does not delete
`.git` as rollback because that would be a broader destructive action than the
requested setup.

## 2. `LocalAgentRunner`

### Interface

`src/coding_agent/local_runner.py` exposes a settings dataclass and one method:

```python
class LocalAgentRunner:
    def run(self, objective: str) -> RunResult:
        ...
```

The constructor accepts validated run settings, workspace, approvals, model
adapter, run directory, and event presentation mode. `run` creates a fresh run
ID, event log, artifact store, tool runtime, and `AgentEngine` for every call.

This removes duplicated per-run composition from `run` and the terminal UI.
It does not own workspace setup, argument parsing, or terminal input.

The one-shot command maps `SUCCEEDED` and `ANSWERED` to process exit code zero,
while the distinct status preserves their semantics in events and reports. The
interactive session records the result and continues regardless of status.

## 3. `ConversationStore` and `ConversationSession`

### Interface

`src/coding_agent/conversation.py` exposes the persistence boundary:

```python
class ConversationStore:
    def create(self, workspace: Path) -> ConversationSession: ...
    def resume(self, reference: str, workspace: Path) -> ConversationSession: ...
    def list_sessions(self, *, workspace: Path | None, limit: int) -> tuple[SessionInfo, ...]: ...

class ConversationSession:
    def prepare(self, request: str) -> PreparedConversation: ...
    def record(self, request: str, result: RunResult) -> None: ...
    def history(self, *, limit: int = 20) -> ConversationHistory: ...
    def resumable_sessions(self, *, limit: int = 20) -> tuple[SessionInfo, ...]: ...
    def switch(self, reference: str) -> ConversationSession: ...
```

The CLI decides where sessions live and supplies a redactor and context limits.
Callers do not parse logs, manage revisions, choose compaction checkpoints, or
construct model context themselves.

### Durable format and replay

Each session uses a random lowercase 32-hex ID and an append-only
`session.jsonl` under the per-user session directory. The first record binds the
schema, ID, canonical workspace, and creation time. Later `turn` and
`compaction` records carry contiguous revisions. Replay validates record types,
field sizes, UTF-8, turn ordering, revision ordering, status values, and an
overall file-size bound before returning state.

Writes take an operating-system file lock, append one bounded JSON record,
flush, and `fsync`. The configured API key and credential-shaped values are
redacted before persistence. A turn stores only the user request, final
assistant outcome, terminal status, run ID, and changed-path names. It never
serializes verification records, approval decisions, tool observations,
controller budgets, or live engine state.

Resume accepts a full ID or a 2–32 character hexadecimal prefix. Prefix
resolution happens inside `ConversationStore`, considers only sessions bound to
the caller's canonical workspace, and succeeds only for one match. Zero matches
and ambiguous matches are distinct errors; ambiguity reports collision-safe
candidate references. Full IDs remain compatible. Thus a valid reference cannot
move historical text into a different target repository. Completed turns are
resumable; a run interrupted before producing a `RunResult` is deliberately not
checkpointed.

`SessionInfo` contains a collision-safe display reference, the last completed
turn timestamp, and the last redacted user request in addition to counts and
workspace. Presentation adapters convert the UTC timestamp to local time and
bound the request to one line; callers never parse JSONL to discover sessions.

### Automatic context compaction

`prepare` derives a prompt view under a hard character budget. It first keeps
recent raw turns. If the rendered view exceeds the target, it folds the oldest
eligible turns into compact structured digests containing bounded request and
outcome fragments, status, run ID, and changed paths. At least two recent turns
remain raw by default. The checkpoint is persisted, so another process rebuilds
the same compacted view without repeating work.

This is deterministic controller compaction, not a recursive model
summarization call. The prompt wraps both compacted memory and recent turns in a
warning that restored text is untrusted context only and cannot grant
permissions, approvals, verification evidence, or repository authority. The
current request and history are jointly hard-bounded before the model call.

## 4. `InteractiveSession`

### Interface

`src/coding_agent/interactive.py` exposes:

```python
class InteractiveSession:
    def run(self, run_task: Callable[[str], RunResult]) -> int:
        ...
```

The constructor accepts a `ConversationSession`, model label, and optional
input/output streams. Tests use a temporary `ConversationStore`, `StringIO`, and
a fake `run_task`; production uses the durable session and
`LocalAgentRunner.run`.

### Loop

1. Render a compact banner and help hint.
2. Read one line from `coding-agent> `.
3. Ignore blanks; dispatch leading `/` commands locally.
4. Ask `ConversationSession.prepare` for the bounded objective.
5. Call `run_task` once.
6. Persist the completed `RunResult` through `ConversationSession.record` and
   print status/run ID.
7. Continue until `/exit`, `/quit`, or EOF.

`/history` reads persisted recent turns and reports how many earlier turns are
in compacted memory. `/session` prints the short reference and full ID.
`/resume` uses the terminal's reusable highlighted-choice interface over
same-workspace `SessionInfo` values; `/resume <prefix>` bypasses the selector.
Switching replaces only `ConversationSession`, because every offered session is
bound to the already-constructed runner's workspace. The banner distinguishes
new from resumed sessions, and exit prints the short resume command.

The terminal uses ANSI styling only when output is a TTY and `NO_COLOR` is not
set. Plain text is the complete fallback, which keeps Windows and redirected
tests deterministic. `/clear` emits ANSI clear-screen codes only in styled
mode.

`TerminalPrompt.readline(prompt) -> str` owns platform key decoding and the
slash-command selector behind one interface. Windows uses `msvcrt.getwch` and
recognizes both Windows extended keys and ConPTY escape sequences. POSIX uses a
temporary raw terminal mode and decodes common ANSI navigation sequences. The
implementation restores terminal mode before returning, so command approvals
continue to use normal line input. Redirected streams bypass key handling and
call `readline` directly.

The prompt maintains a Unicode-aware editable buffer with left/right,
Home/End, Backspace, and Delete behavior. A leading `/` opens the static command
catalog immediately. The menu redraws its fixed-height candidate area in place,
using reverse video for the selected command. Enter closes the menu and copies
the selection into the existing editable buffer; only a later Enter returns the
line to `InteractiveSession`. Selection and prefix filtering stay internal to
the module. Tests inject semantic key events at the same `readline` interface.

`KeyboardInterrupt` while reading a prompt prints a cancellation hint and
returns to the prompt. EOF exits cleanly. A `KeyboardInterrupt` escaping a run
is reported as an interrupted task and does not synthesize a successful
`RunResult`.

## Progress projection and approval

`src/coding_agent/presentation.py` owns bounded descriptions for every tool.
It includes paths, line ranges, entry/match counts, executable, cwd, purpose,
argument count, inline-code length, exit code, and mutation status as
applicable. It never embeds file contents, script bodies, search strings, or
full output. Control/format characters are escaped before reaching the console.

`AgentEngine` adds only these bounded descriptions to `model_action` and
`tool_finished` events. `ConsoleEventSink` renders them and omits empty
rationale punctuation; JSONL and JSON-console event shapes remain additive and
redacted.

Tool timing distinguishes controller/approval latency from execution. The
console prints `execution_ms` and, when nonzero, `approval_wait_ms`; the total
`duration_ms` remains in structured events. The engine skips the second
identical consecutive read-only action and records explicit corrective context,
then terminates a third with `STAGNATION`. Context construction also removes
older byte-identical read observations for the same workspace version so a
weak model cannot fill its window by rereading one file range. Protocol-error
events carry the bounded concrete validation reason through the same redaction
and control-character-safe presentation path.

The model protocol adds `respond(message) -> AnswerRequest`. `AgentEngine`
accepts it as terminal `ANSWERED` only when `workspace_version == 0` and no
changed path was recorded. After mutation it records an `answer_rejected`
observation, returns to `RUNNING`, and requires `finish` with fresh evidence or
`report_blocked`. Coding evaluation continues to pass only `SUCCEEDED` plus a
successful independent Oracle.

Before approval, `LocalToolRuntime` computes the operation digest from the
original arguments, then supplies `PromptApprovalAdapter` with a bounded tool
description and a separately redacted argument object. The adapter shows only
the summary by default. `d` renders the full redacted JSON with visual wrapping
and the full digest, while `y` approves and empty input/`n` denies. The digest,
not the display wrapping, identifies the exact operation.

Existing-file edit, whole-file replacement, and deletion are recovery-backed.
After validating the expected hash and before mutation, `LocalToolRuntime`
stores the exact UTF-8 before-image in the current run artifact store. If
redaction would alter the content or the artifact bound would truncate it, the
runtime rejects the mutation. Tool events and `inspect-run` expose the opaque
recovery ID; `recover-file` copies it to a caller-selected new path using
exclusive creation. Git diff projection synthesizes unified patches for
untracked UTF-8 files, and the controller accepts `finish` only after obtaining
a complete, non-empty final diff artifact.

## CLI integration

`build_parser` exposes `tui` with the same workspace, model, budget, approval,
and allow-program options as `run`, except `task` and `--json`. It also exposes
`resume REFERENCE` with the interactive options and `sessions` for discovery.

`main` normalizes an empty argument list to `tui`, so:

```text
coding-agent              -> interactive session in Path.cwd()
coding-agent tui ...      -> explicit interactive session
coding-agent resume ID    -> resume in Path.cwd()
coding-agent sessions     -> list saved sessions without model credentials
coding-agent run ...      -> one bounded run
```

Configuration validation occurs before `prepare_workspace`; a missing model,
invalid endpoint, invalid budget, or missing API key cannot mutate the target.
For a new TUI, workspace setup precedes session creation. For resume, session
ID and workspace binding are validated before setup, then the existing Git
workspace is prepared as a no-op. `sessions` needs neither a model nor API key.
`inspect-run` and `eval` are unchanged.

Session storage precedence is `sessions_dir` in user settings,
`CODING_AGENT_SESSIONS_DIR`, then `~/.coding-agent/sessions`. The TUI and resume
commands accept `--session-context-chars`; the settings field has the same name
and a validated range of 2,000 through 18,000 characters.

The package remains installable as a console script. Documentation recommends
a one-time `uv tool install --editable <project>` for users who want bare
`coding-agent` available from arbitrary repositories.

## Security analysis

- Bootstrap Git writes are human-requested startup behavior, outside the model
  action loop.
- The model never receives a Git initialization or commit tool.
- Existing repositories are not automatically committed or cleaned.
- Enclosing repositories are rejected to avoid expanding or ambiguously nesting
  the workspace.
- Git subprocesses cannot receive `OPENAI_API_KEY` or other arbitrary inherited
  variables.
- Credential-shaped files are ignored before `git add --all`.
- Commit identity is inherited from Git configuration and never fabricated.
- Setup messages and failures contain bounded Git output and no environment
  dump.
- Initial approval prompts never dump file/script content; expanded details are
  redacted and still bound to the original operation digest.
- Console progress escapes control characters supplied through model arguments
  and bounds every free-form field.
- Objectives and persisted event strings replace unpaired Unicode surrogates at
  their shared boundary, preventing malformed redirected input from crashing
  UTF-8 logs or reaching the model protocol unchanged.
- Persistent session IDs are non-path 32-hex values. User references are
  validated hexadecimal prefixes, prefix resolution is unique and
  workspace-bound, and replay rejects malformed, oversized, reordered, or
  unsupported records.
- Session text is redacted before append; restored turns are explicitly
  untrusted and omit approvals, verification evidence, and controller state.
- Per-session file locking prevents overlapping record writes. Compaction is a
  bounded deterministic controller transform rather than executable content or
  an extra privileged model action.
- A non-mutating answer cannot authorize tools, waive approval, or satisfy a
  coding evaluation; a post-mutation `respond` action is rejected.

## Tests

### Workspace tests

- non-Git directory becomes a repository with one commit and clean status;
- existing source is tracked while `.env`, key files, caches, and build output
  remain untracked/ignored;
- existing `.gitignore` content is preserved and missing rules are appended;
- an existing repository is unchanged and setup is idempotent;
- a subdirectory of an enclosing repository is rejected;
- an invalid `.git` entry and Git command failure become setup errors.

These tests use real temporary Git repositories at the module interface. Git is
a local-substitutable dependency, so no production-shaped subprocess port is
added solely for tests.

### Interactive tests

- bare argument normalization selects `tui`;
- a natural-language line invokes the runner exactly once;
- multiple requests produce separate persisted history entries and bounded
  context;
- a second `InteractiveSession` can resume the ID and receives both the prior
  request and assistant outcome;
- unique short references resolve, ambiguous prefixes are rejected, and session
  summaries expose the last request and timestamp;
- `/resume` selects a same-workspace session and switches context without
  invoking the agent runner;
- old turns compact automatically, the checkpoint survives replay, and the
  current request plus context never exceeds the hard objective bound;
- wrong-workspace and traversal-shaped session IDs are rejected;
- secrets and verification IDs do not enter session JSONL;
- slash commands do not invoke the runner;
- `/` opens a command catalog; arrows visibly highlight candidates, the first
  Enter completes without submitting, and filtering, Backspace, Escape,
  Unicode input, and redirected line mode behave deterministically;
- `/history`, EOF, `/exit`, and `KeyboardInterrupt` behave deterministically;
- plain output contains workspace, result status, and run ID.

### Presentation and approval tests

- each file tool names its path without including content;
- long inline commands report only program, metadata, and code length in the
  initial progress/approval summary;
- tool results report entry counts, changed paths, or exit codes;
- `d` exposes wrapped redacted arguments and the full digest before reprompting;
- empty approval input denies and concise prompts stay bounded.

### Regression and smoke checks

- existing CLI, engine, tool, integration, and evaluation unit tests;
- one-shot `run --help`, explicit `tui --help`, `resume --help`, session listing,
  and bare TUI exit smoke tests;
- a two-process resume smoke test confirms model-visible conversation
  continuity while each turn still creates an independent run;
- automatic initialization smoke test in a temporary non-Git directory;
- `git diff --check` and a final repository status inspection.

The tool schema version advances because `respond` and `ANSWERED` extend the
controller protocol. Run the live evaluation suite when configured and still
require agent `SUCCEEDED`, Oracle exit code `0`, and zero false successes. Also
run a real conversational smoke check that requires `ANSWERED` with no changed
files.
