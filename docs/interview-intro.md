# One-minute English introduction

I built a controller-driven coding agent for local Git repositories. The model
suggests actions, but my controller owns the process. It accepts one action per
turn; if a provider returns several tool calls, the adapter executes only the
first. The controller validates permissions, runs local file and command tools,
records actual results, and feeds them into the next turn. Hash-guarded edits
prevent stale overwrites. Commands require approval, avoid the shell, and never
receive the model API key. The model can only request completion, which the
controller accepts after fresh verification on the current workspace version.
I also built resumable context, offline tests, and an independent evaluation
harness with hidden tests to detect false successes.

