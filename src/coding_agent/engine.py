"""Controller-owned agent loop and terminal-state enforcement."""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from dataclasses import replace
from typing import Any

from .context import ContextManager
from .domain import (
    Action,
    AgentError,
    AnswerRequest,
    AssistantTurn,
    BlockedRequest,
    BudgetExhaustedError,
    Clock,
    ErrorCode,
    EventSink,
    FinishRequest,
    ModelAuthError,
    ModelPort,
    ModelProtocolError,
    ModelRequestError,
    ModelTransientError,
    RiskLevel,
    RunOptions,
    RunResult,
    RunState,
    RunStatus,
    StepRecord,
    TaskRequest,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolStatus,
    VerificationRecord,
    action_arguments,
    action_name,
    replace_unpaired_surrogates,
)
from .events import EventEmitter
from .presentation import describe_tool, describe_tool_result
from .tools.runtime import ToolRuntime


CONTROL_DEFINITIONS = (
    ToolDefinition(
        name="respond",
        description=(
            "Answer an informational or conversational request that is complete "
            "without changing the workspace. This returns ANSWERED, not verified "
            "coding success, and is rejected after any workspace mutation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "minLength": 1},
            },
            "required": ["message"],
            "additionalProperties": False,
        },
        risk=RiskLevel.READ_ONLY,
    ),
    ToolDefinition(
        name="finish",
        description="Request successful completion using fresh verification evidence.",
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "minLength": 1},
                "verification_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "verification_ids"],
            "additionalProperties": False,
        },
        risk=RiskLevel.READ_ONLY,
    ),
    ToolDefinition(
        name="report_blocked",
        description="Stop safely because an external requirement prevents progress.",
        input_schema={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "minLength": 1},
                "needed": {"type": "string"},
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
        risk=RiskLevel.READ_ONLY,
    ),
)

_WEB_FILE_SUFFIXES = (
    ".html",
    ".htm",
    ".css",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".svelte",
)
_VISUAL_OBJECTIVE_TERMS = (
    "web page",
    "web app",
    "website",
    "frontend",
    "user interface",
    "visual",
    "animation",
    "layout",
    "styling",
    "interactive",
    "interaction",
    "canvas",
    "game",
    "网页",
    "界面",
    "视觉",
    "动画",
    "布局",
    "样式",
    "交互",
    "游戏",
    "质感",
    "效果",
)
_READ_CACHE_RECOVERY_THRESHOLD = 2
_READ_ONLY_PROGRESS_THRESHOLD = 8
_WRAP_UP_TURN_RESERVE = 4
_WRAP_UP_TOOL_NAMES = {
    "run_command",
    "browser_check",
    "read_output",
    "search_output",
    "finish",
    "report_blocked",
}
_MODEL_TURN_BUDGET_EXHAUSTED = "model-turn budget exhausted"
_FINALIZATION_MESSAGE = (
    "work-turn budget exhausted; allowing one finish-only decision"
)
_PROGRESS_MESSAGE = (
    "read-only action limit reached; pausing inspection tools until progress"
)
_WRAP_UP_MESSAGE = (
    "work-turn budget is low; switching to verification and completion"
)


class SystemClock(Clock):
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class AgentEngine:
    """A deep module exposing one run operation across the main system seam."""

    def __init__(
        self,
        *,
        model: ModelPort,
        tools: ToolRuntime,
        events: EventSink,
        options: RunOptions | None = None,
        clock: Clock | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.options = options or RunOptions()
        self.clock = clock or SystemClock()
        self.context = ContextManager(self.options)
        self.events = events
        self.run_metadata = dict(run_metadata or {})

    def run(self, request: TaskRequest) -> RunResult:
        run_id = request.run_id or uuid.uuid4().hex
        emitter = EventEmitter(run_id, self.events)
        state = RunState(
            run_id=run_id,
            objective=replace_unpaired_surrogates(request.objective.strip()),
            workspace=request.workspace.resolve(),
        )
        started = self.clock.monotonic()
        if not state.objective:
            return self._terminal(
                state,
                emitter,
                RunStatus.FAILED,
                "task objective must not be empty",
                error_code=ErrorCode.MODEL_REQUEST,
            )
        if len(state.objective) > 20_000:
            return self._terminal(
                state,
                emitter,
                RunStatus.FAILED,
                "task objective exceeds the 20000-character limit",
                error_code=ErrorCode.MODEL_REQUEST,
            )

        baseline = self.tools.initial_workspace_state()
        state.initial_git_head = _optional_string(baseline.get("git_head"))
        state.initial_git_status = str(baseline.get("git_status", ""))
        emitter.emit(
            "run_started",
            objective=state.objective,
            initial_git_head=state.initial_git_head,
            initially_dirty=bool(state.initial_git_status),
            metadata=self.run_metadata,
        )
        if not baseline.get("git_available"):
            return self._terminal(
                state,
                emitter,
                RunStatus.FAILED,
                "workspace must be a Git repository",
                error_code=ErrorCode.TOOL_INPUT,
            )
        state.status = RunStatus.RUNNING

        try:
            while True:
                budget_failure = self._check_budget(state, started)
                if budget_failure and not self._enter_finalization_mode(
                    state,
                    emitter,
                    started,
                    budget_failure,
                ):
                    return self._terminal(
                        state,
                        emitter,
                        RunStatus.FAILED,
                        budget_failure,
                        error_code=ErrorCode.BUDGET_EXHAUSTED,
                    )

                self._update_guidance_modes(state, emitter)

                try:
                    turn = self._request_model(state, emitter, started)
                except BudgetExhaustedError as exc:
                    return self._terminal(
                        state,
                        emitter,
                        RunStatus.FAILED,
                        str(exc),
                        error_code=exc.code,
                    )
                except ModelAuthError as exc:
                    return self._terminal(
                        state,
                        emitter,
                        RunStatus.BLOCKED,
                        str(exc),
                        blocked_reason=str(exc),
                        error_code=exc.code,
                    )
                except (ModelRequestError, ModelTransientError) as exc:
                    return self._terminal(
                        state,
                        emitter,
                        RunStatus.FAILED,
                        str(exc),
                        error_code=exc.code,
                    )
                except ModelProtocolError as exc:
                    state.protocol_errors += 1
                    self._record_protocol_error(state, emitter, str(exc))
                    if state.protocol_errors > self.options.max_protocol_errors:
                        return self._terminal(
                            state,
                            emitter,
                            RunStatus.FAILED,
                            "model repeatedly violated the action protocol",
                            error_code=ErrorCode.MODEL_PROTOCOL,
                        )
                    continue

                state.protocol_errors = 0
                cached_read = self._find_cached_read(state, turn.action)
                repeated_failure = self._track_action_fingerprint(state, turn.action)
                emitter.emit(
                    "model_action",
                    action=action_name(turn.action),
                    detail=(
                        describe_tool(turn.action.name, turn.action.arguments)
                        if isinstance(turn.action, ToolCall)
                        else ""
                    ),
                    rationale=turn.rationale[:1_000],
                    repeated=state.repeated_actions,
                    proposed_actions=max(1, turn.proposed_action_count),
                )
                if state.finalization_mode and not isinstance(
                    turn.action,
                    (FinishRequest, BlockedRequest),
                ):
                    return self._terminal(
                        state,
                        emitter,
                        RunStatus.FAILED,
                        "model returned a non-terminal action during finish-only grace",
                        error_code=ErrorCode.BUDGET_EXHAUSTED,
                    )
                if state.wrap_up_mode and not self._is_wrap_up_action(turn.action):
                    return self._terminal(
                        state,
                        emitter,
                        RunStatus.FAILED,
                        "model returned a work action during verification-only wrap-up",
                        error_code=ErrorCode.MODEL_PROTOCOL,
                    )
                if state.progress_required and self._is_read_only_action(turn.action):
                    return self._terminal(
                        state,
                        emitter,
                        RunStatus.FAILED,
                        "model returned another read-only action while progress was required",
                        error_code=ErrorCode.MODEL_PROTOCOL,
                    )
                if cached_read is not None:
                    self._record_cached_read(state, turn, cached_read, emitter)
                    continue
                if repeated_failure:
                    return self._terminal(
                        state,
                        emitter,
                        RunStatus.FAILED,
                        repeated_failure,
                        error_code=ErrorCode.STAGNATION,
                    )

                if (
                    isinstance(turn.action, ToolCall)
                    and state.repeated_actions > 1
                    and self._is_read_only_tool(turn.action.name)
                ):
                    self._record_duplicate_read(state, turn, emitter)
                    continue

                if isinstance(turn.action, FinishRequest):
                    completed = self._handle_finish(state, turn, emitter)
                    if completed is not None:
                        return completed
                    continue
                if isinstance(turn.action, AnswerRequest):
                    completed = self._handle_answer(state, turn, emitter)
                    if completed is not None:
                        return completed
                    continue
                if isinstance(turn.action, BlockedRequest):
                    details = turn.action.reason
                    if turn.action.needed:
                        details += f" Needed: {turn.action.needed}"
                    return self._terminal(
                        state,
                        emitter,
                        RunStatus.BLOCKED,
                        details,
                        blocked_reason=turn.action.reason,
                    )

                cancelled = self._execute_tool(state, turn, emitter)
                if cancelled:
                    return self._terminal(
                        state,
                        emitter,
                        RunStatus.CANCELLED,
                        "run cancelled during tool execution",
                        error_code=ErrorCode.CANCELLED,
                    )
                last_result = state.steps[-1].result
                if last_result.get("error_code") == ErrorCode.TOOL_INTERNAL.value:
                    return self._terminal(
                        state,
                        emitter,
                        RunStatus.FAILED,
                        "local tool implementation failed",
                        error_code=ErrorCode.TOOL_INTERNAL,
                    )
        except KeyboardInterrupt:
            return self._terminal(
                state,
                emitter,
                RunStatus.CANCELLED,
                "run cancelled by user",
                error_code=ErrorCode.CANCELLED,
            )

    def _request_model(
        self,
        state: RunState,
        emitter: EventEmitter,
        started: float,
    ) -> AssistantTurn:
        tools = self.tools.definitions + CONTROL_DEFINITIONS
        request = self.context.build(state, tools)
        if state.finalization_mode:
            request = replace(
                request,
                tools=tuple(
                    definition
                    for definition in request.tools
                    if definition.name in {"finish", "report_blocked"}
                ),
            )
        elif state.wrap_up_mode:
            request = replace(
                request,
                tools=tuple(
                    definition
                    for definition in request.tools
                    if definition.name in _WRAP_UP_TOOL_NAMES
                ),
            )
        elif state.progress_required:
            request = replace(
                request,
                tools=tuple(
                    definition
                    for definition in request.tools
                    if definition.risk is not RiskLevel.READ_ONLY
                    or definition.name in {"respond", "finish", "report_blocked"}
                ),
            )
        elif self._consecutive_cached_reads(state) >= _READ_CACHE_RECOVERY_THRESHOLD:
            request = replace(
                request,
                tools=tuple(
                    definition for definition in request.tools if definition.name != "read_file"
                ),
            )
        last_error: ModelTransientError | None = None
        for attempt in range(1, self.options.model_retry_attempts + 1):
            budget_failure = self._check_budget(
                state,
                started,
                ignore_model_turns=state.finalization_mode,
            )
            if budget_failure:
                raise BudgetExhaustedError(budget_failure)
            state.model_turns += 1
            try:
                return self.model.complete(request)
            except ModelTransientError as exc:
                last_error = exc
                if attempt >= self.options.model_retry_attempts:
                    break
                delay = min(2 ** (attempt - 1), 8) + random.uniform(0.0, 0.25)
                emitter.emit(
                    "retry",
                    message="temporary model error",
                    attempt=attempt,
                    delay_seconds=round(delay, 2),
                )
                self.clock.sleep(delay)
        assert last_error is not None
        raise last_error

    def _execute_tool(
        self,
        state: RunState,
        turn: AssistantTurn,
        emitter: EventEmitter,
    ) -> bool:
        assert isinstance(turn.action, ToolCall)
        action_id = uuid.uuid4().hex
        emitter.emit("tool_started", action_id=action_id, tool=turn.action.name)
        state.tool_calls += 1
        result = self.tools.execute(turn.action, action_id)

        if result.data.get("workspace_changed"):
            state.workspace_version += 1
            changed = result.data.get("changed_files", [])
            if isinstance(changed, list):
                state.changed_files.update(str(item) for item in changed)

        result = self._attach_verification(state, turn.action, result)
        state.completion_evidence_ready = _completion_evidence_ready(state)
        self._record_tool_progress(state, turn.action.name)
        if result.error_code:
            state.recent_errors.append(f"{result.error_code.value}: {result.message}")
            state.recent_errors = state.recent_errors[-10:]
        state.steps.append(
            StepRecord(
                step=state.model_turns,
                workspace_version=state.workspace_version,
                rationale=turn.rationale,
                action_name=turn.action.name,
                arguments=dict(turn.action.arguments),
                result=result.for_model(),
            )
        )
        emitter.emit(
            "tool_finished",
            action_id=action_id,
            tool=result.tool_name,
            detail=describe_tool_result(
                turn.action.name,
                turn.action.arguments,
                result.data,
            ),
            status=result.status.value,
            error_code=result.error_code.value if result.error_code else None,
            duration_ms=result.duration_ms,
            approval_wait_ms=result.approval_wait_ms,
            execution_ms=result.execution_ms,
            workspace_version=state.workspace_version,
            recovery_output_id=result.data.get("recovery_output_id"),
            recovery_path=result.data.get("recovery_path"),
            screenshot_id=result.data.get("screenshot_id"),
        )
        return result.status is ToolStatus.CANCELLED

    def _attach_verification(
        self,
        state: RunState,
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        if call.name == "run_command" and call.arguments.get("purpose") == "verify":
            exit_code = result.data.get("exit_code")
            command = (
                str(call.arguments.get("program", "")),
                *(str(item) for item in call.arguments.get("args", [])),
            )
            passed = result.status is ToolStatus.COMPLETED and exit_code == 0
            output_id = _optional_string(result.data.get("output_id"))
            kind = "command"
        elif call.name == "browser_check":
            rendered = result.data.get("rendered") is True
            exit_code = 0 if rendered else None
            command = (
                "browser_check",
                str(call.arguments.get("path", "")),
                f"{call.arguments.get('viewport_width', 1280)}x"
                f"{call.arguments.get('viewport_height', 720)}",
            )
            passed = result.status is ToolStatus.COMPLETED and rendered
            output_id = _optional_string(result.data.get("dom_output_id"))
            kind = "browser"
        else:
            return result
        verification_id = uuid.uuid4().hex
        record = VerificationRecord(
            verification_id=verification_id,
            command=command,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            workspace_version=state.workspace_version,
            passed=passed,
            output_id=output_id,
            kind=kind,
        )
        state.verifications.append(record)
        updated_data = dict(result.data)
        updated_data["verification_id"] = verification_id
        return replace(result, data=updated_data)

    def _handle_finish(
        self,
        state: RunState,
        turn: AssistantTurn,
        emitter: EventEmitter,
    ) -> RunResult | None:
        assert isinstance(turn.action, FinishRequest)
        state.status = RunStatus.VERIFYING
        reason = self._completion_rejection(state, turn.action)
        if reason:
            state.status = RunStatus.RUNNING
            state.recent_errors.append(f"{ErrorCode.VERIFICATION_FAILED.value}: {reason}")
            state.recent_errors = state.recent_errors[-10:]
            state.steps.append(
                StepRecord(
                    step=state.model_turns,
                    workspace_version=state.workspace_version,
                    rationale=turn.rationale,
                    action_name="finish",
                    arguments=action_arguments(turn.action),
                    result={
                        "status": "REJECTED",
                        "error_code": ErrorCode.VERIFICATION_FAILED.value,
                        "message": reason,
                    },
                )
            )
            emitter.emit("verification_rejected", reason=reason)
            return None

        diff_call = ToolCall(call_id="controller", name="git_diff", arguments={})
        diff_result = self.tools.execute(diff_call, uuid.uuid4().hex)
        if diff_result.status is not ToolStatus.COMPLETED:
            state.status = RunStatus.RUNNING
            reason = "controller could not inspect the final Git diff"
            state.recent_errors.append(f"{ErrorCode.VERIFICATION_FAILED.value}: {reason}")
            emitter.emit("verification_rejected", reason=reason)
            return None
        output_chars = diff_result.data.get("output_chars")
        artifact_truncated = bool(diff_result.data.get("artifact_truncated", False))
        if not isinstance(output_chars, int) or output_chars <= 0 or artifact_truncated:
            state.status = RunStatus.RUNNING
            reason = "controller could not inspect a complete, non-empty final Git diff"
            state.recent_errors.append(f"{ErrorCode.VERIFICATION_FAILED.value}: {reason}")
            state.recent_errors = state.recent_errors[-10:]
            emitter.emit("verification_rejected", reason=reason)
            return None
        emitter.emit(
            "final_diff_inspected",
            output_id=diff_result.data.get("output_id"),
            output_chars=output_chars,
        )
        warnings = turn.action.warnings
        if _requires_browser_verification(state):
            review_warning = (
                "Browser rendering passed; subjective visual quality still requires "
                "human review of the saved screenshot."
            )
            if review_warning not in warnings:
                warnings = (*warnings, review_warning)
        return self._terminal(
            state,
            emitter,
            RunStatus.SUCCEEDED,
            turn.action.summary,
            warnings=warnings,
        )

    def _handle_answer(
        self,
        state: RunState,
        turn: AssistantTurn,
        emitter: EventEmitter,
    ) -> RunResult | None:
        assert isinstance(turn.action, AnswerRequest)
        if state.workspace_version > 0 or state.changed_files:
            reason = (
                "respond cannot complete a run after a workspace mutation; "
                "use finish with fresh verification evidence or report_blocked"
            )
            state.recent_errors.append(
                f"{ErrorCode.VERIFICATION_FAILED.value}: {reason}"
            )
            state.recent_errors = state.recent_errors[-10:]
            state.steps.append(
                StepRecord(
                    step=state.model_turns,
                    workspace_version=state.workspace_version,
                    rationale=turn.rationale,
                    action_name="respond",
                    arguments=action_arguments(turn.action),
                    result={
                        "status": "REJECTED",
                        "error_code": ErrorCode.VERIFICATION_FAILED.value,
                        "message": reason,
                    },
                )
            )
            emitter.emit("answer_rejected", reason=reason)
            return None
        return self._terminal(
            state,
            emitter,
            RunStatus.ANSWERED,
            turn.action.message,
        )

    @staticmethod
    def _completion_rejection(state: RunState, request: FinishRequest) -> str | None:
        if not state.changed_files:
            return "no workspace file change was recorded"
        if not request.verification_ids:
            return "finish must cite at least one verification record"
        by_id = {record.verification_id: record for record in state.verifications}
        missing = [item for item in request.verification_ids if item not in by_id]
        if missing:
            return "finish cited unknown verification records"
        cited = [by_id[item] for item in request.verification_ids]
        if not any(
            record.passed and record.workspace_version == state.workspace_version
            for record in cited
        ):
            return "no cited successful verification belongs to the current workspace version"
        if _requires_browser_verification(state) and not any(
            record.kind == "browser"
            and record.passed
            and record.workspace_version == state.workspace_version
            for record in cited
        ):
            return (
                "visual web changes require a cited browser verification from "
                "the current workspace version"
            )
        return None

    def _check_budget(
        self,
        state: RunState,
        started: float,
        *,
        ignore_model_turns: bool = False,
    ) -> str | None:
        if not ignore_model_turns and state.model_turns >= self.options.max_model_turns:
            return _MODEL_TURN_BUDGET_EXHAUSTED
        if self.clock.monotonic() - started >= self.options.max_wall_seconds:
            return "wall-clock budget exhausted"
        return None

    def _enter_finalization_mode(
        self,
        state: RunState,
        emitter: EventEmitter,
        started: float,
        budget_failure: str,
    ) -> bool:
        if (
            budget_failure != _MODEL_TURN_BUDGET_EXHAUSTED
            or state.finalization_grace_used
            or not state.completion_evidence_ready
            or self._check_budget(state, started, ignore_model_turns=True) is not None
        ):
            return False
        state.finalization_grace_used = True
        state.finalization_mode = True
        emitter.emit("finalization_started", message=_FINALIZATION_MESSAGE)
        return True

    def _update_guidance_modes(
        self,
        state: RunState,
        emitter: EventEmitter,
    ) -> None:
        if state.finalization_mode:
            return
        remaining = max(0, self.options.max_model_turns - state.model_turns)
        has_workspace_change = state.workspace_version > 0 or bool(state.changed_files)
        if (
            not state.wrap_up_mode
            and has_workspace_change
            and remaining <= _WRAP_UP_TURN_RESERVE
        ):
            state.wrap_up_mode = True
            state.progress_required = False
            emitter.emit("wrap_up_started", message=_WRAP_UP_MESSAGE)
            return
        if (
            not state.wrap_up_mode
            and not state.progress_required
            and state.consecutive_read_only_actions >= _READ_ONLY_PROGRESS_THRESHOLD
        ):
            state.progress_required = True
            emitter.emit("progress_required", message=_PROGRESS_MESSAGE)

    def _record_tool_progress(self, state: RunState, name: str) -> None:
        if self._is_read_only_tool(name):
            state.consecutive_read_only_actions += 1
            return
        state.consecutive_read_only_actions = 0
        state.progress_required = False

    @staticmethod
    def _is_wrap_up_action(action: Action) -> bool:
        if isinstance(action, (FinishRequest, BlockedRequest)):
            return True
        return isinstance(action, ToolCall) and action.name in _WRAP_UP_TOOL_NAMES

    def _is_read_only_action(self, action: Action) -> bool:
        return isinstance(action, ToolCall) and self._is_read_only_tool(action.name)

    def _track_action_fingerprint(self, state: RunState, action: Action) -> str | None:
        encoded = json.dumps(
            {
                "workspace_version": state.workspace_version,
                "name": action_name(action),
                "arguments": action_arguments(action),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()
        if fingerprint == state.last_action_fingerprint:
            state.repeated_actions += 1
        else:
            state.last_action_fingerprint = fingerprint
            state.repeated_actions = 1
        if state.repeated_actions >= self.options.max_repeated_actions:
            return "the model repeated an identical action without progress"
        return None

    @staticmethod
    def _find_cached_read(state: RunState, action: Action) -> dict[str, Any] | None:
        if not isinstance(action, ToolCall) or action.name != "read_file":
            return None
        request = _validated_read_request(action.arguments)
        if request is None:
            return None
        path, requested_start, requested_end = request

        for step in reversed(state.steps):
            if step.workspace_version != state.workspace_version:
                continue
            if step.action_name != "read_file" or step.arguments.get("path") != path:
                continue
            result = step.result
            if result.get("status") != ToolStatus.COMPLETED.value:
                continue
            data = result.get("data")
            if not isinstance(data, dict):
                continue
            cached = _slice_cached_read(data, requested_start, requested_end)
            if cached is not None:
                return cached
        return None

    @staticmethod
    def _record_cached_read(
        state: RunState,
        turn: AssistantTurn,
        cached_data: dict[str, Any],
        emitter: EventEmitter,
    ) -> None:
        assert isinstance(turn.action, ToolCall)
        message = "served from the controller read cache; no filesystem access was needed"
        cached_count = AgentEngine._consecutive_cached_reads(state) + 1
        if cached_count >= _READ_CACHE_RECOVERY_THRESHOLD:
            message += (
                "; read_file will be unavailable for the next decision because repeated "
                "covered reads are not progress—use the available content to edit, search, "
                "verify, respond, or report a concrete blocker"
            )
        result = ToolResult(
            action_id=uuid.uuid4().hex,
            tool_name=turn.action.name,
            status=ToolStatus.COMPLETED,
            message=message,
            data=cached_data,
        )
        state.steps.append(
            StepRecord(
                step=state.model_turns,
                workspace_version=state.workspace_version,
                rationale=turn.rationale,
                action_name=turn.action.name,
                arguments=dict(turn.action.arguments),
                result=result.for_model(),
            )
        )
        emitter.emit(
            "tool_cached",
            tool=turn.action.name,
            detail=describe_tool(turn.action.name, turn.action.arguments),
            reason=message,
        )
        state.consecutive_read_only_actions += 1

    @staticmethod
    def _consecutive_cached_reads(state: RunState) -> int:
        count = 0
        for step in reversed(state.steps):
            if step.workspace_version != state.workspace_version or step.action_name != "read_file":
                break
            result_data = step.result.get("data")
            if not isinstance(result_data, dict) or result_data.get("cached") is not True:
                break
            count += 1
        return count

    def _is_read_only_tool(self, name: str) -> bool:
        return any(
            definition.name == name and definition.risk is RiskLevel.READ_ONLY
            for definition in self.tools.definitions
        )

    @staticmethod
    def _record_duplicate_read(
        state: RunState,
        turn: AssistantTurn,
        emitter: EventEmitter,
    ) -> None:
        assert isinstance(turn.action, ToolCall)
        message = (
            "identical read-only action skipped because the workspace and request "
            "are unchanged; use the existing observation and choose a different action"
        )
        state.recent_errors.append(f"{ErrorCode.STAGNATION.value}: {message}")
        state.recent_errors = state.recent_errors[-10:]
        state.steps.append(
            StepRecord(
                step=state.model_turns,
                workspace_version=state.workspace_version,
                rationale=turn.rationale,
                action_name=turn.action.name,
                arguments=dict(turn.action.arguments),
                result={
                    "tool": turn.action.name,
                    "status": "SKIPPED",
                    "error_code": ErrorCode.STAGNATION.value,
                    "message": message,
                },
            )
        )
        emitter.emit(
            "tool_skipped",
            tool=turn.action.name,
            detail=describe_tool(turn.action.name, turn.action.arguments),
            reason=message,
        )
        state.consecutive_read_only_actions += 1

    @staticmethod
    def _record_protocol_error(
        state: RunState,
        emitter: EventEmitter,
        message: str,
    ) -> None:
        state.recent_errors.append(f"{ErrorCode.MODEL_PROTOCOL.value}: {message}")
        state.recent_errors = state.recent_errors[-10:]
        state.steps.append(
            StepRecord(
                step=state.model_turns,
                workspace_version=state.workspace_version,
                rationale="",
                action_name="protocol_error",
                arguments={},
                result={
                    "status": "REJECTED",
                    "error_code": ErrorCode.MODEL_PROTOCOL.value,
                    "message": message,
                },
            )
        )
        emitter.emit(
            "warning",
            message=f"model action protocol rejected: {message[:500]}",
        )

    @staticmethod
    def _terminal(
        state: RunState,
        emitter: EventEmitter,
        status: RunStatus,
        summary: str,
        *,
        warnings: tuple[str, ...] = (),
        blocked_reason: str | None = None,
        error_code: ErrorCode | None = None,
    ) -> RunResult:
        state.status = status
        emitter.emit(
            "terminal",
            status=status.value,
            summary=summary,
            changed_files=sorted(state.changed_files),
            error_code=error_code.value if error_code else None,
            warnings=list(warnings),
        )
        return RunResult(
            run_id=state.run_id,
            status=status,
            summary=summary,
            changed_files=tuple(sorted(state.changed_files)),
            verifications=tuple(state.verifications),
            warnings=warnings,
            blocked_reason=blocked_reason,
            error_code=error_code,
            model_turns=state.model_turns,
            tool_calls=state.tool_calls,
            workspace_version=state.workspace_version,
        )


def _validated_read_request(
    arguments: dict[str, Any],
) -> tuple[str, int, int | None] | None:
    if set(arguments) - {"path", "start_line", "end_line"}:
        return None
    path = arguments.get("path")
    start_line = arguments.get("start_line", 1)
    end_line = arguments.get("end_line")
    if not isinstance(path, str) or not path:
        return None
    if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1:
        return None
    if end_line is not None and (
        not isinstance(end_line, int)
        or isinstance(end_line, bool)
        or end_line < start_line
    ):
        return None
    return path, start_line, end_line


def _slice_cached_read(
    data: dict[str, Any],
    requested_start: int,
    requested_end: int | None,
) -> dict[str, Any] | None:
    content = data.get("content")
    cached_start = data.get("start_line")
    cached_end = data.get("end_line")
    observed_lines = data.get("observed_lines")
    if not isinstance(content, str):
        return None
    if (
        not isinstance(cached_start, int)
        or isinstance(cached_start, bool)
        or cached_start < 1
        or not isinstance(cached_end, int)
        or isinstance(cached_end, bool)
        or cached_end < 0
        or not isinstance(observed_lines, int)
        or isinstance(observed_lines, bool)
        or observed_lines < 0
        or cached_start > requested_start
    ):
        return None

    reaches_eof = data.get("truncated") is False and cached_end >= observed_lines
    if requested_end is None:
        if not reaches_eof:
            return None
        actual_end = observed_lines
    elif requested_end <= cached_end:
        actual_end = requested_end
    elif reaches_eof:
        actual_end = observed_lines
    else:
        return None

    start_offset = requested_start - cached_start
    end_offset = max(start_offset, actual_end - cached_start + 1)
    lines = content.splitlines(keepends=True)
    if start_offset > len(lines) or end_offset > len(lines):
        return None

    cached_data = dict(data)
    cached_data.update(
        {
            "content": "".join(lines[start_offset:end_offset]),
            "start_line": requested_start,
            "end_line": actual_end,
            "cached": True,
        }
    )
    return cached_data


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _requires_browser_verification(state: RunState) -> bool:
    if not any(
        path.casefold().endswith(_WEB_FILE_SUFFIXES) for path in state.changed_files
    ):
        return False
    current_objective = state.objective.rsplit(
        "\nCurrent request:\n", 1
    )[-1].casefold()
    return any(term in current_objective for term in _VISUAL_OBJECTIVE_TERMS)


def _completion_evidence_ready(state: RunState) -> bool:
    if not state.changed_files:
        return False
    current_passes = [
        record
        for record in state.verifications
        if record.passed and record.workspace_version == state.workspace_version
    ]
    if not current_passes:
        return False
    if _requires_browser_verification(state):
        return any(record.kind == "browser" for record in current_passes)
    return True
