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
                if budget_failure:
                    return self._terminal(
                        state,
                        emitter,
                        RunStatus.FAILED,
                        budget_failure,
                        error_code=ErrorCode.BUDGET_EXHAUSTED,
                    )

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
                )
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
        last_error: ModelTransientError | None = None
        for attempt in range(1, self.options.model_retry_attempts + 1):
            budget_failure = self._check_budget(state, started)
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
        )
        return result.status is ToolStatus.CANCELLED

    def _attach_verification(
        self,
        state: RunState,
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        if call.name != "run_command" or call.arguments.get("purpose") != "verify":
            return result
        verification_id = uuid.uuid4().hex
        exit_code = result.data.get("exit_code")
        record = VerificationRecord(
            verification_id=verification_id,
            command=(
                str(call.arguments.get("program", "")),
                *(str(item) for item in call.arguments.get("args", [])),
            ),
            exit_code=exit_code if isinstance(exit_code, int) else None,
            workspace_version=state.workspace_version,
            passed=result.status is ToolStatus.COMPLETED and exit_code == 0,
            output_id=_optional_string(result.data.get("output_id")),
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
        return self._terminal(
            state,
            emitter,
            RunStatus.SUCCEEDED,
            turn.action.summary,
            warnings=turn.action.warnings,
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
        return None

    def _check_budget(self, state: RunState, started: float) -> str | None:
        if state.model_turns >= self.options.max_model_turns:
            return "model-turn budget exhausted"
        if self.clock.monotonic() - started >= self.options.max_wall_seconds:
            return "wall-clock budget exhausted"
        return None

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


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
