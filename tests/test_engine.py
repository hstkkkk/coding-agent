from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from coding_agent.domain import (
    AnswerRequest,
    AssistantTurn,
    BlockedRequest,
    ErrorCode,
    FinishRequest,
    RiskLevel,
    RunOptions,
    RunStatus,
    TaskRequest,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolStatus,
)
from coding_agent.engine import AgentEngine
from coding_agent.events import InMemoryEventSink
from coding_agent.model import ScriptedModelAdapter
from coding_agent.domain import ModelProtocolError, ModelTransientError


TOOL_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": True}


class FakeToolRuntime:
    def __init__(
        self,
        *,
        diff_chars: int = 10,
        diff_truncated: bool = False,
        changed_file: str = "app.py",
    ) -> None:
        self.definitions = (
            ToolDefinition("read_file", "read", TOOL_SCHEMA, RiskLevel.READ_ONLY),
            ToolDefinition("search_text", "search", TOOL_SCHEMA, RiskLevel.READ_ONLY),
            ToolDefinition("edit_file", "edit", TOOL_SCHEMA, RiskLevel.WORKSPACE_WRITE),
            ToolDefinition("run_command", "run", TOOL_SCHEMA, RiskLevel.EXECUTION),
            ToolDefinition("browser_check", "render", TOOL_SCHEMA, RiskLevel.EXECUTION),
            ToolDefinition("git_status", "status", TOOL_SCHEMA, RiskLevel.READ_ONLY),
            ToolDefinition("git_diff", "diff", TOOL_SCHEMA, RiskLevel.READ_ONLY),
        )
        self.calls: list[str] = []
        self.diff_chars = diff_chars
        self.diff_truncated = diff_truncated
        self.changed_file = changed_file
        self.read_content = "".join(f"line {line}\n" for line in range(1, 410))

    def initial_workspace_state(self):
        return {"git_available": True, "git_head": "abc", "git_status": ""}

    def execute(self, call: ToolCall, action_id: str) -> ToolResult:
        self.calls.append(call.name)
        if call.name == "read_file":
            lines = self.read_content.splitlines(keepends=True)
            start_line = int(call.arguments.get("start_line", 1))
            requested_end = call.arguments.get("end_line")
            end_line = int(requested_end) if requested_end is not None else len(lines)
            selected = "".join(lines[start_line - 1 : end_line])
            return ToolResult(
                action_id,
                call.name,
                ToolStatus.COMPLETED,
                "tool completed",
                data={
                    "path": str(call.arguments.get("path", "")),
                    "content": selected,
                    "sha256": "f" * 64,
                    "start_line": start_line,
                    "end_line": min(end_line, len(lines)),
                    "observed_lines": len(lines),
                    "truncated": False,
                },
            )
        if call.name == "edit_file":
            return ToolResult(
                action_id,
                call.name,
                ToolStatus.COMPLETED,
                "edited",
                data={"workspace_changed": True, "changed_files": [self.changed_file]},
            )
        if call.name == "run_command":
            return ToolResult(
                action_id,
                call.name,
                ToolStatus.COMPLETED,
                "completed",
                data={
                    "exit_code": 0,
                    "output_id": "a" * 32,
                    "workspace_changed": False,
                    "changed_files": [self.changed_file],
                },
            )
        if call.name == "browser_check":
            return ToolResult(
                action_id,
                call.name,
                ToolStatus.COMPLETED,
                "rendered",
                data={
                    "rendered": True,
                    "screenshot_id": "b" * 32,
                    "dom_output_id": "c" * 32,
                    "path": "index.html",
                },
            )
        return ToolResult(
            action_id,
            call.name,
            ToolStatus.COMPLETED,
            "diff",
            data={
                "output_id": "d" * 32,
                "output_chars": self.diff_chars,
                "truncated": self.diff_truncated,
                "artifact_truncated": self.diff_truncated,
            },
        )


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def edit_turn() -> AssistantTurn:
    return AssistantTurn(
        "make minimal edit",
        ToolCall("c1", "edit_file", {"path": "app.py"}),
    )


def verify_turn() -> AssistantTurn:
    return AssistantTurn(
        "run targeted tests",
        ToolCall("c2", "run_command", {"program": "python", "args": ["-m", "unittest"], "purpose": "verify"}),
    )


def finish_from_context(request) -> AssistantTurn:
    match = re.search(r'"verification_id": "([a-f0-9]{32})"', request.user_prompt)
    if not match:
        raise AssertionError("verification id was not present in model context")
    return AssistantTurn(
        "finish with evidence",
        FinishRequest("c3", "task completed", (match.group(1),)),
    )


def finish_with_latest_evidence(request) -> AssistantTurn:
    matches = re.findall(r'"verification_id": "([a-f0-9]{32})"', request.user_prompt)
    if not matches:
        raise AssertionError("verification id was not present in model context")
    return AssistantTurn(
        "finish with browser evidence",
        FinishRequest("visual-finish", "visual task completed", (matches[-1],)),
    )


class AgentEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_engine(
        self,
        responses,
        *,
        options: RunOptions | None = None,
        clock=None,
        objective: str = "fix the bug",
        tools: FakeToolRuntime | None = None,
    ):
        model = ScriptedModelAdapter(responses)
        tools = tools or FakeToolRuntime()
        events = InMemoryEventSink()
        engine = AgentEngine(model=model, tools=tools, events=events, options=options, clock=clock)
        result = engine.run(TaskRequest(objective, self.workspace))
        return result, model, tools, events

    def test_success_requires_fresh_cited_verification(self) -> None:
        result, _, tools, events = self.run_engine([edit_turn(), verify_turn(), finish_from_context])

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.changed_files, ("app.py",))
        self.assertTrue(result.verifications[0].passed)
        self.assertEqual(tools.calls, ["edit_file", "run_command", "git_diff"])
        self.assertEqual(events.events[-1].kind, "terminal")
        model_events = [event for event in events.events if event.kind == "model_action"]
        tool_events = [event for event in events.events if event.kind == "tool_finished"]
        self.assertEqual(model_events[0].data["detail"], "path=app.py")
        self.assertEqual(tool_events[0].data["detail"], "path=app.py · changed")

    def test_pure_conversation_returns_answered_without_workspace_change(self) -> None:
        result, model, tools, events = self.run_engine(
            [AssistantTurn("answer directly", AnswerRequest("answer", "我是编程智能体。"))]
        )

        self.assertEqual(result.status, RunStatus.ANSWERED)
        self.assertEqual(result.summary, "我是编程智能体。")
        self.assertEqual(result.changed_files, ())
        self.assertEqual(tools.calls, [])
        self.assertEqual(events.events[-1].data["status"], "ANSWERED")
        self.assertIn("respond", {tool.name for tool in model.requests[0].tools})

    def test_serialized_proposal_count_is_recorded_without_extra_execution(self) -> None:
        result, _, tools, events = self.run_engine(
            [
                AssistantTurn(
                    "answer directly",
                    AnswerRequest("answer", "Only the first action is used."),
                    proposed_action_count=2,
                )
            ]
        )

        self.assertEqual(result.status, RunStatus.ANSWERED)
        self.assertEqual(tools.calls, [])
        model_event = next(
            event for event in events.events if event.kind == "model_action"
        )
        self.assertEqual(model_event.data["proposed_actions"], 2)

    def test_objective_replaces_unpaired_surrogates_before_model_request(self) -> None:
        malformed = "Who are you?" + chr(0xDC81)

        def answer_from_clean_context(request) -> AssistantTurn:
            self.assertNotIn(chr(0xDC81), request.user_prompt)
            self.assertIn("\N{REPLACEMENT CHARACTER}", request.user_prompt)
            return AssistantTurn("answer", AnswerRequest("answer", "A coding agent."))

        result, _, _, _ = self.run_engine(
            [answer_from_clean_context],
            objective=malformed,
        )

        self.assertEqual(result.status, RunStatus.ANSWERED)

    def test_answer_is_rejected_after_workspace_mutation(self) -> None:
        result, _, tools, events = self.run_engine(
            [
                edit_turn(),
                AssistantTurn("answer instead", AnswerRequest("answer", "done")),
                AssistantTurn(
                    "cannot verify",
                    BlockedRequest("blocked", "verification unavailable"),
                ),
            ]
        )

        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(tools.calls, ["edit_file"])
        self.assertTrue(any(event.kind == "answer_rejected" for event in events.events))

    def test_rejects_finish_without_evidence_then_allows_blocked(self) -> None:
        result, _, _, events = self.run_engine(
            [
                edit_turn(),
                AssistantTurn("done", FinishRequest("c2", "done", ())),
                AssistantTurn("cannot verify", BlockedRequest("c3", "tests unavailable")),
            ]
        )

        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertTrue(any(event.kind == "verification_rejected" for event in events.events))

    def test_finish_rejects_an_empty_or_truncated_final_diff(self) -> None:
        for tools in (
            FakeToolRuntime(diff_chars=0),
            FakeToolRuntime(diff_chars=20, diff_truncated=True),
        ):
            with self.subTest(diff_chars=tools.diff_chars, truncated=tools.diff_truncated):
                result, _, _, events = self.run_engine(
                    [
                        edit_turn(),
                        verify_turn(),
                        finish_from_context,
                        AssistantTurn(
                            "cannot produce an inspectable diff",
                            BlockedRequest("blocked", "final diff unavailable"),
                        ),
                    ],
                    tools=tools,
                )

                self.assertEqual(result.status, RunStatus.BLOCKED)
                reasons = [
                    event.data.get("reason", "")
                    for event in events.events
                    if event.kind == "verification_rejected"
                ]
                self.assertTrue(any("final Git diff" in reason for reason in reasons))

    def test_visual_web_change_requires_cited_browser_verification(self) -> None:
        tools = FakeToolRuntime(changed_file="index.html")
        result, _, _, events = self.run_engine(
            [
                edit_turn(),
                verify_turn(),
                finish_from_context,
                AssistantTurn(
                    "render the current page",
                    ToolCall("browser", "browser_check", {"path": "index.html"}),
                ),
                finish_with_latest_evidence,
            ],
            objective="改进网页版沙子游戏的视觉效果、动画和交互",
            tools=tools,
        )

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual([item.kind for item in result.verifications], ["command", "browser"])
        self.assertTrue(any("subjective visual quality" in item for item in result.warnings))
        self.assertIn("browser_check", tools.calls)
        reasons = [
            event.data.get("reason", "")
            for event in events.events
            if event.kind == "verification_rejected"
        ]
        self.assertTrue(any("browser" in reason for reason in reasons))

    def test_old_verification_becomes_stale_after_another_edit(self) -> None:
        result, _, _, _ = self.run_engine(
            [
                edit_turn(),
                verify_turn(),
                edit_turn(),
                finish_from_context,
                AssistantTurn("cannot rerun", BlockedRequest("c5", "verification unavailable")),
            ]
        )
        self.assertEqual(result.status, RunStatus.BLOCKED)

    def test_repeated_protocol_errors_fail(self) -> None:
        result, _, _, events = self.run_engine(
            [
                ModelProtocolError("bad one"),
                ModelProtocolError("bad two"),
                ModelProtocolError("bad three"),
            ],
            options=RunOptions(max_protocol_errors=2),
        )
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.MODEL_PROTOCOL)
        warnings = [event.data.get("message", "") for event in events.events if event.kind == "warning"]
        self.assertTrue(any("bad one" in message for message in warnings))

    def test_transient_model_error_retries_with_fake_clock(self) -> None:
        clock = FakeClock()
        result, model, _, _ = self.run_engine(
            [
                ModelTransientError("temporary"),
                AssistantTurn("blocked", BlockedRequest("c2", "external dependency")),
            ],
            clock=clock,
        )
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(len(clock.sleeps), 1)

    def test_retry_cannot_exceed_model_turn_budget(self) -> None:
        clock = FakeClock()
        result, _, _, _ = self.run_engine(
            [ModelTransientError("temporary")],
            options=RunOptions(max_model_turns=1),
            clock=clock,
        )
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.BUDGET_EXHAUSTED)

    def test_current_verification_is_marked_ready_for_completion(self) -> None:
        def finish_when_ready(request) -> AssistantTurn:
            self.assertIn('"completion_evidence_ready": true', request.user_prompt)
            self.assertIn('"finalization_mode": false', request.user_prompt)
            return finish_from_context(request)

        result, _, _, _ = self.run_engine(
            [edit_turn(), verify_turn(), finish_when_ready]
        )

        self.assertEqual(result.status, RunStatus.SUCCEEDED)

    def test_final_verification_can_use_the_last_work_turn_then_finish_in_grace(self) -> None:
        tools = FakeToolRuntime(changed_file="index.html")

        def finish_in_grace(request) -> AssistantTurn:
            self.assertEqual(
                {tool.name for tool in request.tools},
                {"finish", "report_blocked"},
            )
            self.assertIn('"completion_evidence_ready": true', request.user_prompt)
            self.assertIn('"finalization_mode": true', request.user_prompt)
            return finish_with_latest_evidence(request)

        result, model, _, events = self.run_engine(
            [
                edit_turn(),
                verify_turn(),
                AssistantTurn(
                    "render the current page",
                    ToolCall("browser", "browser_check", {"path": "index.html"}),
                ),
                finish_in_grace,
            ],
            options=RunOptions(max_model_turns=3),
            objective="改进网页版沙子游戏的视觉效果、动画和交互",
            tools=tools,
        )

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.model_turns, 4)
        self.assertEqual(len(model.requests), 4)
        self.assertEqual(
            len([event for event in events.events if event.kind == "finalization_started"]),
            1,
        )

    def test_budget_has_no_finalization_grace_without_fresh_evidence(self) -> None:
        result, model, _, events = self.run_engine(
            [edit_turn()],
            options=RunOptions(max_model_turns=1),
        )

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.BUDGET_EXHAUSTED)
        self.assertEqual(len(model.requests), 1)
        self.assertFalse(any(event.kind == "finalization_started" for event in events.events))

    def test_rejected_finish_in_grace_cannot_reopen_the_work_loop(self) -> None:
        result, model, _, events = self.run_engine(
            [
                edit_turn(),
                verify_turn(),
                AssistantTurn(
                    "finish with invalid evidence",
                    FinishRequest("bad-finish", "done", ("0" * 32,)),
                ),
            ],
            options=RunOptions(max_model_turns=2),
        )

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.BUDGET_EXHAUSTED)
        self.assertEqual(result.model_turns, 3)
        self.assertEqual(len(model.requests), 3)
        self.assertTrue(any(event.kind == "verification_rejected" for event in events.events))

    def test_finalization_grace_never_executes_another_work_action(self) -> None:
        result, model, tools, events = self.run_engine(
            [
                edit_turn(),
                verify_turn(),
                AssistantTurn(
                    "try another edit",
                    ToolCall("late-edit", "edit_file", {"path": "app.py"}),
                ),
            ],
            options=RunOptions(max_model_turns=2),
        )

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.BUDGET_EXHAUSTED)
        self.assertEqual(result.model_turns, 3)
        self.assertEqual(len(model.requests), 3)
        self.assertEqual(tools.calls, ["edit_file", "run_command"])
        self.assertTrue(any(event.kind == "finalization_started" for event in events.events))

    def test_wrap_up_reserves_tail_turns_for_current_visual_verification(self) -> None:
        tools = FakeToolRuntime(changed_file="index.html")
        inspected = 0

        def inspect_or_complete(request) -> AssistantTurn:
            nonlocal inspected
            available = {tool.name for tool in request.tools}
            if "read_file" in available:
                inspected += 1
                return AssistantTurn(
                    "inspect another range",
                    ToolCall(
                        f"read-{inspected}",
                        "read_file",
                        {"path": f"view-{inspected}.html"},
                    ),
                )
            self.assertIn('"wrap_up_mode": true', request.user_prompt)
            self.assertEqual(
                available,
                {"run_command", "browser_check", "finish", "report_blocked"},
            )
            if '"completion_evidence_ready": true' in request.user_prompt:
                return finish_with_latest_evidence(request)
            return AssistantTurn(
                "verify the final rendering",
                ToolCall("browser", "browser_check", {"path": "index.html"}),
            )

        result, _, runtime, events = self.run_engine(
            [edit_turn(), *([inspect_or_complete] * 6)],
            options=RunOptions(max_model_turns=6),
            objective="让网页沙子动画呈现连续、细腻的视觉效果",
            tools=tools,
        )

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertIn("browser_check", runtime.calls)
        self.assertLess(inspected, 5)
        self.assertEqual(
            len([event for event in events.events if event.kind == "wrap_up_started"]),
            1,
        )

    def test_cross_tool_read_only_streak_requires_a_progress_decision(self) -> None:
        inspections = [
            AssistantTurn("inspect", ToolCall("read-1", "read_file", {"path": "a.py"})),
            AssistantTurn("status", ToolCall("status-1", "git_status", {})),
            AssistantTurn("inspect", ToolCall("read-2", "read_file", {"path": "b.py"})),
            AssistantTurn(
                "search",
                ToolCall("search-1", "search_text", {"path": ".", "query": "alpha"}),
            ),
            AssistantTurn("inspect", ToolCall("read-3", "read_file", {"path": "c.py"})),
            AssistantTurn("status", ToolCall("status-2", "git_status", {})),
            AssistantTurn(
                "search",
                ToolCall("search-2", "search_text", {"path": ".", "query": "beta"}),
            ),
            AssistantTurn("inspect", ToolCall("read-4", "read_file", {"path": "d.py"})),
        ]

        def edit_when_focused(request) -> AssistantTurn:
            available = {tool.name for tool in request.tools}
            self.assertNotIn("read_file", available)
            self.assertNotIn("search_text", available)
            self.assertNotIn("git_status", available)
            self.assertNotIn("git_diff", available)
            self.assertIn("edit_file", available)
            self.assertIn('"progress_required": true', request.user_prompt)
            return edit_turn()

        result, _, _, events = self.run_engine(
            [*inspections, edit_when_focused, verify_turn(), finish_from_context]
        )

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(
            len([event for event in events.events if event.kind == "progress_required"]),
            1,
        )

    def test_wrap_up_rejects_a_late_edit_without_executing_it(self) -> None:
        result, _, tools, events = self.run_engine(
            [edit_turn(), edit_turn()],
            options=RunOptions(max_model_turns=2),
        )

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.MODEL_PROTOCOL)
        self.assertEqual(tools.calls, ["edit_file"])
        self.assertTrue(any(event.kind == "wrap_up_started" for event in events.events))

    def test_identical_action_stagnation_stops_before_third_execution(self) -> None:
        same = AssistantTurn("inspect", ToolCall("same", "git_diff", {}))
        result, model, tools, events = self.run_engine([same, same, same])

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.STAGNATION)
        self.assertEqual(tools.calls, ["git_diff"])
        self.assertIn("unchanged", model.requests[2].user_prompt)
        self.assertTrue(any(event.kind == "tool_skipped" for event in events.events))

    def test_covered_read_ranges_are_cached_and_force_a_progress_decision(self) -> None:
        def edit_after_recovery_prompt(request) -> AssistantTurn:
            available = {tool.name for tool in request.tools}
            self.assertNotIn("read_file", available)
            self.assertIn("served from the controller read cache", request.user_prompt)
            self.assertIn('"content": "line 150\\nline 151\\n', request.user_prompt)
            self.assertIn('line 409\\n"', request.user_prompt)
            return edit_turn()

        result, _, tools, events = self.run_engine(
            [
                AssistantTurn("read file", ToolCall("r1", "read_file", {"path": "app.py"})),
                AssistantTurn(
                    "read middle",
                    ToolCall(
                        "r2",
                        "read_file",
                        {"path": "app.py", "start_line": 150, "end_line": 260},
                    ),
                ),
                AssistantTurn(
                    "read overlapping tail",
                    ToolCall(
                        "r3",
                        "read_file",
                        {"path": "app.py", "start_line": 150, "end_line": 409},
                    ),
                ),
                edit_after_recovery_prompt,
                verify_turn(),
                finish_from_context,
            ]
        )

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(tools.calls.count("read_file"), 1)
        self.assertEqual(
            len([event for event in events.events if event.kind == "tool_cached"]),
            2,
        )

    def test_read_cache_does_not_cross_a_workspace_mutation(self) -> None:
        result, _, tools, _ = self.run_engine(
            [
                AssistantTurn("read file", ToolCall("r1", "read_file", {"path": "app.py"})),
                edit_turn(),
                AssistantTurn("read changed file", ToolCall("r2", "read_file", {"path": "app.py"})),
                verify_turn(),
                finish_from_context,
            ]
        )

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(tools.calls.count("read_file"), 2)

    def test_read_cache_does_not_hide_an_unobserved_range(self) -> None:
        result, _, tools, events = self.run_engine(
            [
                AssistantTurn(
                    "read first range",
                    ToolCall(
                        "r1",
                        "read_file",
                        {"path": "app.py", "start_line": 1, "end_line": 100},
                    ),
                ),
                AssistantTurn(
                    "read second range",
                    ToolCall(
                        "r2",
                        "read_file",
                        {"path": "app.py", "start_line": 101, "end_line": 200},
                    ),
                ),
                AssistantTurn("answer", AnswerRequest("answer", "inspection complete")),
            ]
        )

        self.assertEqual(result.status, RunStatus.ANSWERED)
        self.assertEqual(tools.calls.count("read_file"), 2)
        self.assertFalse(any(event.kind == "tool_cached" for event in events.events))


if __name__ == "__main__":
    unittest.main()
