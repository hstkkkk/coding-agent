# One-minute English introduction

I built a controller-driven coding agent for small tasks in local Git
repositories. Instead of letting the language model control the process, the
model can propose only one structured action per turn. My controller validates
permissions, executes local file or command tools, records the real result, and
feeds that observation into the next turn. File edits use hashes to prevent
stale overwrites, while commands require explicit approval and run without a
shell or access to the model API key. Most importantly, the model can only
request completion. The controller accepts it after successful verification on
the current workspace version. I also built an offline test suite and an
independent evaluation harness with hidden tests to measure false successes.

