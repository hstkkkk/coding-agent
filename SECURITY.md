# Security model

This project reduces accidental model-driven damage; it is not a strong
sandbox. Approved subprocesses run with the current operating-system user's
permissions.

## Controls

- workspace-relative path validation after canonicalization;
- rejection of direct `.git`, `.coding-agent`, and likely credential-file access;
- hash-guarded edits and explicit create/replace/delete operations;
- exact, bounded before-image recovery artifacts for edit, replacement, and deletion,
  with the mutation rejected when safe complete storage is unavailable;
- no shell interpretation for normal commands;
- interactive approval for command execution, whole-file replacement, and
  deletion, including an explicit warning for pre-existing untracked targets;
- bounded approval summaries with on-demand redacted arguments bound to the
  exact operation digest;
- explicit, executable-scoped pre-approval for automated runs;
- filtered child-process environment without the model API key;
- bounded model turns, process time, file reads, search results, and outputs;
- best-effort secret redaction before content reaches logs or model context;
- replacement of unpaired Unicode surrogates before model-request and UTF-8
  event-log boundaries;
- bounded progress descriptions that omit file contents, script bodies, search
  strings, and raw command arguments;
- read-only Git tools and rejection of common Git history/write operations;
- trusted pre-run Git initialization with credential-protecting ignore rules
  and a filtered subprocess environment;
- bounded, strictly validated per-user settings loaded outside the target
  repository, with API-key values excluded from object representations and
  validation errors;
- workspace-bound persistent conversation IDs with bounded UTF-8 JSONL replay,
  contiguous revisions, file locking, and redaction before append;
- deterministic context compaction that restores only labeled untrusted
  requests/outcomes, never approval decisions, verification evidence, tool
  state, or an in-flight controller;
- process-tree termination on timeout or cancellation;
- approved headless-browser rendering with a disposable profile, disabled
  background services, bounded time and PNG size, and no model-visible local
  screenshot path.

Recovery artifacts use the same restricted run storage and redaction boundary
as other artifacts. Because a redacted before-image would not be faithful, an
existing-file mutation is rejected when secret filtering would alter the
content. `recover-file` writes only to a new destination and refuses to
overwrite an existing path.

Browser rendering executes the target page's JavaScript. It therefore uses the
same explicit execution approval boundary as other local code. Hostname
resolution is disabled for the render and the profile is temporary, but this
is not a network or OS sandbox; direct network addresses and resources allowed
by the operating system remain a non-goal. Screenshots may contain sensitive
page content. They stay in restricted local run storage, are identified to the
model only by an opaque ID, and are copied out only through the exclusive-create
`export-screenshot` command.

`ANSWERED` is deliberately separate from verified coding success. The
controller accepts `respond` only before any recorded workspace mutation, and
the evaluator never treats `ANSWERED` as a passing coding result. This prevents
a model from editing files and then using a conversational terminal action to
bypass fresh verification.

Automatic `git init` and the initial baseline commit occur only in the trusted
CLI startup path after configuration preflight. Existing repositories are not
committed, cleaned, or rewritten. These Git writes are never available to the
model as tools.

## Non-goals

The current release does not provide container, virtual-machine,
operating-system account, or firewall isolation. A repository that the user
approves for code execution may read or modify anything permitted to the
current account through mechanisms the controller cannot observe. Run
untrusted repositories only in an external sandbox.

Secret-pattern filtering is defense in depth, not a complete data-loss
prevention system. Do not place credentials in the target workspace. Store the
fixed `~/.coding-agent/settings.json` file with user-only operating-system
permissions when it contains `api_key`.

Conversation logs under `~/.coding-agent/sessions` are also local sensitive
data: redaction is best-effort and requests may contain proprietary source or
business context. Limit access with operating-system account permissions and do
not treat the session directory as a safe place to paste credentials.

The model-facing file tools cannot open `.coding-agent/settings.json`, but an
approved subprocess runs with the current account's permissions and can access
resources that account can access. Do not approve execution in an untrusted
repository merely because the file-tool boundary blocks the settings path.

