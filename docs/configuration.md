# User configuration

`coding-agent` reads one optional, fixed per-user file before parsing a normal
command:

```text
Windows: C:\Users\<you>\.coding-agent\settings.json
Other:   ~/.coding-agent/settings.json
```

There are no project-local settings and no flag that changes this path. A
missing file is valid and leaves environment variables and built-in defaults
in effect. `--help` remains available even when the file is malformed.

## Example

```json
{
  "api_key": "<your-api-key>",
  "model": "deepseek-v4-flash",
  "base_url": "https://api.deepseek.com",
  "thinking": "disabled",
  "max_turns": 30,
  "max_seconds": 900,
  "command_timeout": 120,
  "approval_mode": "prompt",
  "allow_programs": ["python"],
  "runs_dir": "runs",
  "sessions_dir": "sessions",
  "session_context_chars": 12000
}
```

Relative `runs_dir` and `sessions_dir` values are resolved against the
directory containing `settings.json`, so the example stores both kinds of logs
under `~/.coding-agent`.

## Schema

Unknown fields, wrong types, out-of-range numbers, non-UTF-8 input, malformed
JSON, and files larger than 64 KiB are rejected before workspace setup or a
model request.

| Field | Type and allowed values | Fallback |
|---|---|---|
| `api_key` | non-empty string | environment variable selected by `api_key_env` |
| `api_key_env` | environment-variable name | `OPENAI_API_KEY` |
| `model` | non-empty string | `CODING_AGENT_MODEL`; otherwise required |
| `base_url` | absolute HTTP(S) URL | `CODING_AGENT_BASE_URL`, then `https://api.openai.com/v1` |
| `thinking` | `"enabled"`, `"disabled"`, or `null` | `CODING_AGENT_THINKING`, then provider default |
| `max_turns` | integer from 1 to 200 | `30` |
| `max_seconds` | integer from 1 to 7200 | `900` |
| `command_timeout` | integer from 1 to 600 | `120` |
| `approval_mode` | `"prompt"` or `"deny"` | `"prompt"` |
| `allow_programs` | array of PATH-resolved executable names | empty array |
| `runs_dir` | non-empty absolute or settings-relative path | `CODING_AGENT_RUNS_DIR`, then `~/.coding-agent/runs` |
| `sessions_dir` | non-empty absolute or settings-relative path | `CODING_AGENT_SESSIONS_DIR`, then `~/.coding-agent/sessions` |
| `session_context_chars` | integer from 2000 to 18000 | `12000` |

An explicit JSON `null` for `thinking` means “use the provider default” and
overrides `CODING_AGENT_THINKING`. Other nullable-looking fields must be
omitted rather than set to `null`.

With `thinking` set to `"enabled"`, the HTTP adapter uses automatic rather than
forced tool selection because Thinking-capable endpoints may reject
`tool_choice="required"`. The local protocol remains strict: a response with
zero, multiple, malformed, or unknown actions is rejected before execution.

## Precedence

For a setting that has all four forms, the order is:

1. an explicit command-line option;
2. a value present in `settings.json`;
3. its environment variable;
4. the built-in default.

Repeated `--allow-program NAME` options replace the `allow_programs` array for
that invocation. There is deliberately no `--api-key` option because command
arguments may be visible to other local processes. A direct `api_key` in the
file takes priority over the environment-key fallback.

Evaluation suites keep their own `allowed_programs` list so an evaluation's
execution policy stays reproducible; the user-level `allow_programs` field
applies to `tui` and `run`.

`session_context_chars` applies to `tui` and `resume` and can be overridden by
`--session-context-chars`. It bounds only restored conversation history; each
individual agent run retains its own controller-owned prompt and event limits.

## Security boundary

The file contains a plain-text credential when `api_key` is used. Keep it
outside target repositories and limit access with operating-system account
permissions. The loader never includes the key value in validation errors, and
the settings object's representation omits it.

Model-facing file tools reject direct access to any `.coding-agent` directory,
including when a user's home directory is selected as the workspace. This is a
workspace-level control, not an operating-system sandbox: code launched through
an approved subprocess still has the current user's permissions and can access
paths allowed by the operating system.
