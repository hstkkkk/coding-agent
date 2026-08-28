"""Deep local-tool module: validate, authorize, execute, and normalize."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from ..artifacts import ArtifactStore
from ..domain import (
    ApprovalPort,
    ApprovalRequest,
    ErrorCode,
    RiskLevel,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolStatus,
)
from ..events import Redactor
from ..policy import PathPolicy, PolicyViolation, needs_approval, operation_digest
from .filesystem import EditConflict, FileTools, ToolInputError
from .git import GitInspector
from .process import ProcessRunner


class ToolRuntime(Protocol):
    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return model-visible tool definitions."""

    def execute(self, call: ToolCall, action_id: str) -> ToolResult:
        """Execute exactly one validated tool call."""

    def initial_workspace_state(self) -> dict[str, Any]:
        """Return controller-owned Git baseline information."""


class LocalToolRuntime(ToolRuntime):
    def __init__(
        self,
        *,
        workspace: Path,
        approvals: ApprovalPort,
        artifacts: ArtifactStore,
        redactor: Redactor,
        default_command_timeout: int = 120,
    ) -> None:
        self.path_policy = PathPolicy(workspace)
        self.approvals = approvals
        self.artifacts = artifacts
        self.redactor = redactor
        self.default_command_timeout = default_command_timeout
        self.files = FileTools(self.path_policy)
        self.git = GitInspector(self.path_policy.workspace)
        self.processes = ProcessRunner()
        self._definitions = _definitions()
        self._definition_by_name = {item.name: item for item in self._definitions}
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            "list_directory": self.files.list_directory,
            "read_file": self.files.read_file,
            "search_text": self.files.search_text,
            "edit_file": self.files.edit_file,
            "create_file": self.files.create_file,
            "delete_file": self.files.delete_file,
            "run_command": self._run_command,
            "git_status": self._git_status,
            "git_diff": self._git_diff,
            "read_output": self._read_output,
            "search_output": self._search_output,
        }

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    def initial_workspace_state(self) -> dict[str, Any]:
        snapshot = self.git.snapshot()
        return {
            "git_available": snapshot.available,
            "git_head": snapshot.head,
            "git_status": snapshot.status,
            "changed_files": list(snapshot.changed_files),
        }

    def execute(self, call: ToolCall, action_id: str) -> ToolResult:
        started = time.monotonic()
        definition = self._definition_by_name.get(call.name)
        if definition is None:
            return self._result(
                action_id,
                call.name,
                ToolStatus.REJECTED,
                "unknown tool",
                ErrorCode.TOOL_INPUT,
                started,
            )
        try:
            _validate_arguments(definition.input_schema, call.arguments)
            if call.name == "run_command":
                self._check_command_policy(call.arguments)
            if needs_approval(definition.risk):
                digest = operation_digest(call.name, call.arguments)
                summary = self.redactor.text(
                    f"{call.name} {json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)}"
                )
                decision = self.approvals.request(
                    request=ApprovalRequest(
                        action_id,
                        call.name,
                        definition.risk,
                        summary,
                        digest,
                        dict(call.arguments),
                    )
                )
                if not decision.approved:
                    return self._result(
                        action_id,
                        call.name,
                        ToolStatus.REJECTED,
                        decision.reason or "operation was not approved",
                        ErrorCode.APPROVAL_DENIED,
                        started,
                    )

            data = self._handlers[call.name](**call.arguments)
            status = ToolStatus.TIMED_OUT if data.pop("_timed_out", False) else ToolStatus.COMPLETED
            error_code = ErrorCode.TOOL_TIMEOUT if status is ToolStatus.TIMED_OUT else None
            exit_code = data.get("exit_code")
            if status is ToolStatus.COMPLETED and isinstance(exit_code, int) and exit_code != 0:
                error_code = ErrorCode.COMMAND_FAILED
            truncated = bool(data.get("truncated", False))
            message = str(data.pop("_message", "tool completed"))
            sanitized = self.redactor.value(data)
            assert isinstance(sanitized, dict)
            return ToolResult(
                action_id=action_id,
                tool_name=call.name,
                status=status,
                message=message,
                data=sanitized,
                error_code=error_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                truncated=truncated,
            )
        except PolicyViolation as exc:
            return self._result(
                action_id,
                call.name,
                ToolStatus.REJECTED,
                str(exc),
                ErrorCode.POLICY_DENIED,
                started,
            )
        except EditConflict as exc:
            return self._result(
                action_id,
                call.name,
                ToolStatus.CONFLICT,
                str(exc),
                ErrorCode.TOOL_CONFLICT,
                started,
            )
        except (ToolInputError, ValueError, TypeError) as exc:
            return self._result(
                action_id,
                call.name,
                ToolStatus.REJECTED,
                str(exc),
                ErrorCode.TOOL_INPUT,
                started,
            )
        except KeyboardInterrupt:
            return self._result(
                action_id,
                call.name,
                ToolStatus.CANCELLED,
                "operation was cancelled",
                ErrorCode.CANCELLED,
                started,
            )
        except Exception as exc:  # defensive normalization at the tool seam
            return self._result(
                action_id,
                call.name,
                ToolStatus.INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
                ErrorCode.TOOL_INTERNAL,
                started,
            )

    @staticmethod
    def _result(
        action_id: str,
        tool_name: str,
        status: ToolStatus,
        message: str,
        error_code: ErrorCode,
        started: float,
    ) -> ToolResult:
        return ToolResult(
            action_id=action_id,
            tool_name=tool_name,
            status=status,
            message=message,
            error_code=error_code,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _run_command(
        self,
        *,
        program: str,
        args: list[str],
        cwd: str = ".",
        timeout_seconds: int | None = None,
        purpose: str = "inspect",
    ) -> dict[str, Any]:
        target_cwd = self.path_policy.resolve(cwd)
        if not target_cwd.is_dir():
            raise ToolInputError("cwd is not a directory")
        timeout = timeout_seconds or self.default_command_timeout
        before = self.git.snapshot()
        execution = self.processes.run(
            program=program,
            args=args,
            cwd=target_cwd,
            timeout_seconds=timeout,
        )
        after = self.git.snapshot()
        combined = (
            "--- stdout ---\n"
            f"{execution.stdout}\n"
            "--- stderr ---\n"
            f"{execution.stderr}"
        )
        artifact = self.artifacts.write_text(combined)
        changed = before.status != after.status or before.head != after.head
        if not before.available or not after.available:
            changed = True
        preview = _preview(self.redactor.text(combined), 8_000)
        return {
            "program": program,
            "args": args,
            "cwd": target_cwd.relative_to(self.path_policy.workspace).as_posix() or ".",
            "purpose": purpose,
            "exit_code": execution.exit_code,
            "stdout_preview": preview,
            "output_id": artifact.output_id,
            "output_chars": artifact.original_chars,
            "truncated": execution.output_truncated or artifact.truncated,
            "workspace_changed": changed,
            "changed_files": list(after.changed_files),
            "_timed_out": execution.timed_out,
            "_message": "command timed out" if execution.timed_out else "command completed",
        }

    def _git_status(self) -> dict[str, Any]:
        snapshot = self.git.snapshot()
        if not snapshot.available:
            raise ToolInputError("workspace is not a Git repository")
        return {
            "head": snapshot.head,
            "status": snapshot.status,
            "changed_files": list(snapshot.changed_files),
        }

    def _git_diff(self, *, staged: bool = False) -> dict[str, Any]:
        diff = self.redactor.text(self.git.diff(staged=staged))
        artifact = self.artifacts.write_text(diff)
        return {
            "staged": staged,
            "diff_preview": _preview(diff, 12_000),
            "output_id": artifact.output_id,
            "output_chars": artifact.original_chars,
            "truncated": artifact.truncated or len(diff) > 12_000,
        }

    def _read_output(self, *, output_id: str, offset: int = 0, limit: int = 8_000) -> dict[str, Any]:
        if limit > 20_000:
            raise ToolInputError("limit must not exceed 20000")
        text, total = self.artifacts.read_text(output_id, offset, limit)
        return {
            "output_id": output_id,
            "offset": offset,
            "content": text,
            "total_chars": total,
            "truncated": offset + len(text) < total,
        }

    def _search_output(
        self,
        *,
        output_id: str,
        pattern: str,
        max_results: int = 50,
    ) -> dict[str, Any]:
        if max_results > 200:
            raise ToolInputError("max_results must not exceed 200")
        matches = self.artifacts.search(output_id, pattern, max_results)
        return {"output_id": output_id, "matches": matches, "truncated": len(matches) >= max_results}

    @staticmethod
    def _check_command_policy(arguments: dict[str, Any]) -> None:
        program = str(arguments.get("program", "")).lower()
        args = arguments.get("args", [])
        first_arg = str(args[0]).lower() if isinstance(args, list) and args else ""
        if program in {"rm", "rmdir", "del", "format", "shutdown", "reboot"}:
            raise PolicyViolation(f"program is blocked by command policy: {program}")
        if program in {"git", "git.exe"} and first_arg in {
            "branch",
            "checkout",
            "cherry-pick",
            "clean",
            "commit",
            "merge",
            "push",
            "rebase",
            "reset",
            "switch",
            "tag",
        }:
            raise PolicyViolation(f"Git write operation is blocked: git {first_arg}")


def _preview(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n...[preview truncated]...\n" + text[-half:]


def _validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    if not isinstance(arguments, dict):
        raise ToolInputError("tool arguments must be an object")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    for name in required:
        if name not in arguments:
            raise ToolInputError(f"missing required argument: {name}")
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ToolInputError(f"unknown arguments: {', '.join(unknown)}")
    for name, value in arguments.items():
        rule = properties.get(name)
        if not rule:
            continue
        _validate_value(name, value, rule)


def _validate_value(name: str, value: Any, rule: dict[str, Any]) -> None:
    expected = rule.get("type")
    valid = True
    if expected == "string":
        valid = isinstance(value, str)
    elif expected == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "boolean":
        valid = isinstance(value, bool)
    elif expected == "array":
        valid = isinstance(value, list)
    elif expected == "object":
        valid = isinstance(value, dict)
    if not valid:
        raise ToolInputError(f"argument {name} must have type {expected}")
    if "enum" in rule and value not in rule["enum"]:
        raise ToolInputError(f"argument {name} has an unsupported value")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in rule and value < rule["minimum"]:
            raise ToolInputError(f"argument {name} is below its minimum")
        if "maximum" in rule and value > rule["maximum"]:
            raise ToolInputError(f"argument {name} exceeds its maximum")
    if isinstance(value, str) and "minLength" in rule and len(value) < rule["minLength"]:
        raise ToolInputError(f"argument {name} is too short")
    if isinstance(value, list) and "items" in rule:
        for index, item in enumerate(value):
            _validate_value(f"{name}[{index}]", item, rule["items"])


def _object_schema(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _definitions() -> tuple[ToolDefinition, ...]:
    string = {"type": "string"}
    path = {"type": "string", "minLength": 1}
    return (
        ToolDefinition(
            "list_directory",
            "List files or directories inside the workspace.",
            _object_schema(
                {
                    "path": path,
                    "recursive": {"type": "boolean"},
                    "max_entries": {"type": "integer", "minimum": 1, "maximum": 2000},
                }
            ),
            RiskLevel.READ_ONLY,
        ),
        ToolDefinition(
            "read_file",
            "Read a UTF-8 file range and return its content hash.",
            _object_schema(
                {
                    "path": path,
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                required=["path"],
            ),
            RiskLevel.READ_ONLY,
        ),
        ToolDefinition(
            "search_text",
            "Search literal text in UTF-8 workspace files.",
            _object_schema(
                {
                    "query": {"type": "string", "minLength": 1},
                    "path": path,
                    "glob": string,
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                required=["query"],
            ),
            RiskLevel.READ_ONLY,
        ),
        ToolDefinition(
            "edit_file",
            "Replace one exact text occurrence if the file hash is still current.",
            _object_schema(
                {
                    "path": path,
                    "old_text": {"type": "string", "minLength": 1},
                    "new_text": string,
                    "expected_sha256": {"type": "string", "minLength": 64},
                },
                required=["path", "old_text", "new_text", "expected_sha256"],
            ),
            RiskLevel.WORKSPACE_WRITE,
        ),
        ToolDefinition(
            "create_file",
            "Create a new UTF-8 file; fail if the path already exists.",
            _object_schema({"path": path, "content": string}, required=["path", "content"]),
            RiskLevel.WORKSPACE_WRITE,
        ),
        ToolDefinition(
            "delete_file",
            "Delete one file if its content hash is still current.",
            _object_schema(
                {"path": path, "expected_sha256": {"type": "string", "minLength": 64}},
                required=["path", "expected_sha256"],
            ),
            RiskLevel.DELETE,
        ),
        ToolDefinition(
            "run_command",
            "Run one executable without a shell in a workspace directory.",
            _object_schema(
                {
                    "program": {"type": "string", "minLength": 1},
                    "args": {"type": "array", "items": string},
                    "cwd": path,
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
                    "purpose": {"type": "string", "enum": ["inspect", "verify", "operate"]},
                },
                required=["program", "args"],
            ),
            RiskLevel.EXECUTION,
        ),
        ToolDefinition(
            "git_status",
            "Return the current read-only Git status and HEAD.",
            _object_schema({}),
            RiskLevel.READ_ONLY,
        ),
        ToolDefinition(
            "git_diff",
            "Return a read-only Git diff preview and output artifact id.",
            _object_schema({"staged": {"type": "boolean"}}),
            RiskLevel.READ_ONLY,
        ),
        ToolDefinition(
            "read_output",
            "Read a bounded range from a prior output artifact.",
            _object_schema(
                {
                    "output_id": {"type": "string", "minLength": 32},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20000},
                },
                required=["output_id"],
            ),
            RiskLevel.READ_ONLY,
        ),
        ToolDefinition(
            "search_output",
            "Search literal text in a prior output artifact.",
            _object_schema(
                {
                    "output_id": {"type": "string", "minLength": 32},
                    "pattern": {"type": "string", "minLength": 1},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                required=["output_id", "pattern"],
            ),
            RiskLevel.READ_ONLY,
        ),
    )
