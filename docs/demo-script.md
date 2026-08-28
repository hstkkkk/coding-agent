# Two-minute demo outline

## 0–15 seconds: task and baseline

- Show the neutral target repository window.
- Run the existing unit test and show the deterministic failure.
- State the task in one sentence.

## 15–30 seconds: architecture

Show only this control path:

```text
CLI -> AgentEngine <-> Model
                -> Policy -> Local tools -> Workspace
                <- Tool results and verification evidence
```

## 30–85 seconds: real loop

- Start one agent run.
- Show concise events: read, search, hash-guarded edit, verification command.
- Accelerate waiting time without hiding inputs or results.

## 85–105 seconds: evidence

- Show the final Git diff.
- Show the passing verification and current workspace version.
- Show `SUCCEEDED` only after the completion gate accepts the evidence.

## 105–120 seconds: design claim

Explain that the model proposes actions while the controller owns permissions,
execution, retries, budgets, and terminal status. State plainly that the current
version provides workspace controls rather than an operating-system sandbox.

## Recording hygiene

- neutral terminal prompt and working directory;
- no account avatar, browser session, notification, or personal bookmark;
- no name, institution, email, Git identity, or local absolute path;
- no API key, environment dump, or raw model request;
- inspect repository files and commit metadata before recording.

