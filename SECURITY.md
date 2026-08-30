# Security model

This project reduces accidental model-driven damage; it is not a strong
sandbox. Approved subprocesses run with the current operating-system user's
permissions.

## Controls

- workspace-relative path validation after canonicalization;
- rejection of direct `.git` and likely credential-file access;
- hash-guarded edits and explicit create/delete operations;
- no shell interpretation for normal commands;
- interactive approval for command execution and deletion;
- explicit, executable-scoped pre-approval for automated runs;
- filtered child-process environment without the model API key;
- bounded model turns, process time, file reads, search results, and outputs;
- best-effort secret redaction before content reaches logs or model context;
- read-only Git tools and rejection of common Git history/write operations;
- trusted pre-run Git initialization with credential-protecting ignore rules
  and a filtered subprocess environment;
- process-tree termination on timeout or cancellation.

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
prevention system. Do not place credentials in the target workspace.

