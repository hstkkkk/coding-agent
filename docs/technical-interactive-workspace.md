# Technical design: interactive CLI and Git workspace bootstrap

## Design constraints

The implementation must preserve the current controller contract:

- `AgentEngine` remains the only module that decides run terminal state;
- each model turn still normalizes to exactly one action;
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
  +--> InteractiveSession.run(run_task) -> int
          |
          +--> terminal input/output + bounded session history
```

These are deep modules: callers learn one operation while Git recovery,
per-run composition, command parsing, history bounds, and presentation remain
local to their implementations.

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

Exact existing pattern lines are not duplicated. A missing set is appended
under `# Added by coding-agent workspace setup`. Writes use a same-directory
temporary file and `os.replace`, preserving existing content and newline
termination.

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

The one-shot command maps `RunResult.status` to the existing process exit code.
The interactive session records the result and continues regardless of status.

## 3. `InteractiveSession`

### Interface

`src/coding_agent/interactive.py` exposes:

```python
class InteractiveSession:
    def run(self, run_task: Callable[[str], RunResult]) -> int:
        ...
```

The constructor accepts workspace/model labels plus input and output text
streams. Tests use `StringIO` and a fake `run_task`; production uses the
terminal and `LocalAgentRunner.run`.

### Loop

1. Render a compact banner and help hint.
2. Read one line from `coding-agent> `.
3. Ignore blanks; dispatch leading `/` commands locally.
4. Convert a natural-language request plus bounded history into the objective.
5. Call `run_task` once.
6. Store an `InteractiveHistoryEntry` and print status/run ID.
7. Continue until `/exit`, `/quit`, or EOF.

History retains at most six entries and at most 4,000 contextual characters.
Only prior user requests, terminal statuses, run IDs, and changed paths enter
the next objective. Prior verification IDs and model summaries never carry
forward. Historical text is labeled as context, not instructions or evidence.

The terminal uses ANSI styling only when output is a TTY and `NO_COLOR` is not
set. Plain text is the complete fallback, which keeps Windows and redirected
tests deterministic. `/clear` emits ANSI clear-screen codes only in styled
mode.

`KeyboardInterrupt` while reading a prompt prints a cancellation hint and
returns to the prompt. EOF exits cleanly. A `KeyboardInterrupt` escaping a run
is reported as an interrupted task and does not synthesize a successful
`RunResult`.

## CLI integration

`build_parser` gains an explicit `tui` subcommand with the same workspace,
model, budget, approval, and allow-program options as `run`, except `task` and
`--json`.

`main` normalizes an empty argument list to `tui`, so:

```text
coding-agent              -> interactive session in Path.cwd()
coding-agent tui ...      -> explicit interactive session
coding-agent run ...      -> one bounded run
```

Configuration validation occurs before `prepare_workspace`; a missing model,
invalid endpoint, invalid budget, or missing API key cannot mutate the target.
After validation, both `run` and `tui` call the same workspace setup module and
render its messages. `inspect-run` and `eval` are unchanged.

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
- multiple requests produce separate history entries and bounded context;
- slash commands do not invoke the runner;
- `/history`, EOF, `/exit`, and `KeyboardInterrupt` behave deterministically;
- plain output contains workspace, result status, and run ID.

### Regression and smoke checks

- existing CLI, engine, tool, integration, and evaluation unit tests;
- one-shot `run --help`, explicit `tui --help`, and bare TUI exit smoke tests;
- automatic initialization smoke test in a temporary non-Git directory;
- `git diff --check` and a final repository status inspection.

The live model evaluation is not required by this change because the model
adapter, controller loop, tool protocol, and evaluator semantics remain
unchanged. A live smoke run may be performed separately when credentials and
cost authorization are available.
