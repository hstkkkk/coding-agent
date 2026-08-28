# Bounded Coding Agent

An experimental command-line coding agent that operates on one local Git
workspace at a time. A language model may propose one structured action per
turn; a local controller validates and executes that action, records the
result, and requires fresh verification evidence before accepting completion.

The project intentionally keeps its first release small: one agent loop,
serial tools, workspace-confined file access, explicit approval for command
execution, and an auditable event log.

## Development status

The implementation is under active development. The default test suite is
offline and does not require a model API key.

