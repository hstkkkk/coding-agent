"""Stable domain types shared by the agent's modules.

Vendor SDK objects, subprocess objects, and CLI presentation concerns stop at
their adapters and do not enter this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, TypeAlias


JsonObject: TypeAlias = dict[str, Any]


class RunStatus(str, Enum):
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    ANSWERED = "ANSWERED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ToolStatus(str, Enum):
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CONFLICT = "CONFLICT"
    TIMED_OUT = "TIMED_OUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class RiskLevel(str, Enum):
    READ_ONLY = "READ_ONLY"
    WORKSPACE_WRITE = "WORKSPACE_WRITE"
    OVERWRITE = "OVERWRITE"
    DELETE = "DELETE"
    EXECUTION = "EXECUTION"


class ErrorCode(str, Enum):
    MODEL_TRANSIENT = "MODEL_TRANSIENT"
    MODEL_AUTH = "MODEL_AUTH"
    MODEL_PROTOCOL = "MODEL_PROTOCOL"
    MODEL_REQUEST = "MODEL_REQUEST"
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    TOOL_INPUT = "TOOL_INPUT"
    TOOL_CONFLICT = "TOOL_CONFLICT"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_INTERNAL = "TOOL_INTERNAL"
    COMMAND_FAILED = "COMMAND_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    STALE_VERIFICATION = "STALE_VERIFICATION"
    STAGNATION = "STAGNATION"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: JsonObject
    risk: RiskLevel


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: JsonObject


@dataclass(frozen=True, slots=True)
class AnswerRequest:
    call_id: str
    message: str


@dataclass(frozen=True, slots=True)
class FinishRequest:
    call_id: str
    summary: str
    verification_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BlockedRequest:
    call_id: str
    reason: str
    needed: str = ""


Action: TypeAlias = ToolCall | AnswerRequest | FinishRequest | BlockedRequest


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    rationale: str
    action: Action
    plan_update: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    action_id: str
    tool_name: str
    status: ToolStatus
    message: str
    data: JsonObject = field(default_factory=dict)
    error_code: ErrorCode | None = None
    duration_ms: int = 0
    approval_wait_ms: int = 0
    execution_ms: int = 0
    truncated: bool = False

    def for_model(self) -> JsonObject:
        return {
            "action_id": self.action_id,
            "tool": self.tool_name,
            "status": self.status.value,
            "message": self.message,
            "data": self.data,
            "error_code": self.error_code.value if self.error_code else None,
            "duration_ms": self.duration_ms,
            "approval_wait_ms": self.approval_wait_ms,
            "execution_ms": self.execution_ms,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    action_id: str
    tool_name: str
    risk: RiskLevel
    summary: str
    operation_digest: str
    arguments: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approved: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    verification_id: str
    command: tuple[str, ...]
    exit_code: int | None
    workspace_version: int
    passed: bool
    output_id: str | None = None


@dataclass(frozen=True, slots=True)
class StepRecord:
    step: int
    workspace_version: int
    rationale: str
    action_name: str
    arguments: JsonObject
    result: JsonObject


@dataclass(slots=True)
class RunState:
    run_id: str
    objective: str
    workspace: Path
    status: RunStatus = RunStatus.INITIALIZING
    model_turns: int = 0
    tool_calls: int = 0
    workspace_version: int = 0
    changed_files: set[str] = field(default_factory=set)
    verifications: list[VerificationRecord] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    recent_errors: list[str] = field(default_factory=list)
    protocol_errors: int = 0
    repeated_actions: int = 0
    last_action_fingerprint: str | None = None
    initial_git_status: str = ""
    initial_git_head: str | None = None


@dataclass(frozen=True, slots=True)
class TaskRequest:
    objective: str
    workspace: Path
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunOptions:
    max_model_turns: int = 30
    max_wall_seconds: int = 900
    max_protocol_errors: int = 2
    max_repeated_actions: int = 3
    model_retry_attempts: int = 3
    default_command_timeout: int = 120
    max_context_chars: int = 60_000
    recent_step_limit: int = 12


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    status: RunStatus
    summary: str
    changed_files: tuple[str, ...]
    verifications: tuple[VerificationRecord, ...]
    warnings: tuple[str, ...] = ()
    blocked_reason: str | None = None
    error_code: ErrorCode | None = None
    model_turns: int = 0
    tool_calls: int = 0
    workspace_version: int = 0


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: str
    sequence: int
    kind: str
    timestamp: str
    data: JsonObject


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system_prompt: str
    user_prompt: str
    tools: tuple[ToolDefinition, ...]


class ModelPort(Protocol):
    def complete(self, request: ModelRequest) -> AssistantTurn:
        """Return exactly one normalized action."""


class ApprovalPort(Protocol):
    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        """Approve or reject one operation digest."""


class EventSink(Protocol):
    def emit(self, event: RunEvent) -> None:
        """Persist or present one sanitized run event."""


class Clock(Protocol):
    def monotonic(self) -> float:
        """Return a monotonic timestamp."""

    def sleep(self, seconds: float) -> None:
        """Wait before a retry."""


class AgentError(Exception):
    """Base exception carrying a stable failure code."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code


class ModelTransientError(AgentError):
    def __init__(self, message: str):
        super().__init__(ErrorCode.MODEL_TRANSIENT, message)


class ModelAuthError(AgentError):
    def __init__(self, message: str):
        super().__init__(ErrorCode.MODEL_AUTH, message)


class ModelProtocolError(AgentError):
    def __init__(self, message: str):
        super().__init__(ErrorCode.MODEL_PROTOCOL, message)


class ModelRequestError(AgentError):
    def __init__(self, message: str):
        super().__init__(ErrorCode.MODEL_REQUEST, message)


class BudgetExhaustedError(AgentError):
    def __init__(self, message: str):
        super().__init__(ErrorCode.BUDGET_EXHAUSTED, message)


def action_name(action: Action) -> str:
    if isinstance(action, AnswerRequest):
        return "respond"
    if isinstance(action, FinishRequest):
        return "finish"
    if isinstance(action, BlockedRequest):
        return "report_blocked"
    return action.name


def action_arguments(action: Action) -> JsonObject:
    if isinstance(action, AnswerRequest):
        return {"message": action.message}
    if isinstance(action, FinishRequest):
        return {
            "summary": action.summary,
            "verification_ids": list(action.verification_ids),
            "warnings": list(action.warnings),
        }
    if isinstance(action, BlockedRequest):
        return {"reason": action.reason, "needed": action.needed}
    return dict(action.arguments)


def require_object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelProtocolError(f"{label} must be a JSON object")
    return value


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelProtocolError(f"{label} must be a non-empty string")
    return value.strip()


def string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ModelProtocolError(f"{label} must be an array of strings")
    if not all(isinstance(item, str) for item in value):
        raise ModelProtocolError(f"{label} must contain only strings")
    return tuple(value)


def replace_unpaired_surrogates(value: str) -> str:
    """Return Unicode scalar text that is safe for UTF-8 and JSON boundaries."""

    if not any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        return value
    return "".join(
        "\N{REPLACEMENT CHARACTER}"
        if 0xD800 <= ord(character) <= 0xDFFF
        else character
        for character in value
    )
