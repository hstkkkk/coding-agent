from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from coding_agent.domain import (
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
    def __init__(self) -> None:
        self.definitions = (
            ToolDefinition("edit_file", "edit", TOOL_SCHEMA, RiskLevel.WORKSPACE_WRITE),
            ToolDefinition("run_command", "run", TOOL_SCHEMA, RiskLevel.EXECUTION),
            ToolDefinition("git_diff", "diff", TOOL_SCHEMA, RiskLevel.READ_ONLY),
        )
        self.calls: list[str] = []

    def initial_workspace_state(self):
        return {"git_available": True, "git_head": "abc", "git_status": ""}

    def execute(self, call: ToolCall, action_id: str) -> ToolResult:
        self.calls.append(call.name)
        if call.name == "edit_file":
            return ToolResult(
                action_id,
                call.name,
                ToolStatus.COMPLETED,
                "edited",
                data={"workspace_changed": True, "changed_files": ["app.py"]},
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
                    "changed_files": ["app.py"],
                },
            )
        return ToolResult(action_id, call.name, ToolStatus.COMPLETED, "diff", data={})


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
    return AssistantTurn("make minimal edit", ToolCall("c1", "edit_file", {}))


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


class AgentEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_engine(self, responses, *, options: RunOptions | None = None, clock=None):
        model = ScriptedModelAdapter(responses)
        tools = FakeToolRuntime()
        events = InMemoryEventSink()
        engine = AgentEngine(model=model, tools=tools, events=events, options=options, clock=clock)
        result = engine.run(TaskRequest("fix the bug", self.workspace))
        return result, model, tools, events

    def test_success_requires_fresh_cited_verification(self) -> None:
        result, _, tools, events = self.run_engine([edit_turn(), verify_turn(), finish_from_context])

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.changed_files, ("app.py",))
        self.assertTrue(result.verifications[0].passed)
        self.assertEqual(tools.calls, ["edit_file", "run_command", "git_diff"])
        self.assertEqual(events.events[-1].kind, "terminal")

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
        result, _, _, _ = self.run_engine(
            [
                ModelProtocolError("bad one"),
                ModelProtocolError("bad two"),
                ModelProtocolError("bad three"),
            ],
            options=RunOptions(max_protocol_errors=2),
        )
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.error_code, ErrorCode.MODEL_PROTOCOL)

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

    def test_identical_action_stagnation_stops_before_third_execution(self) -> None:
        same = AssistantTurn("inspect", ToolCall("same", "git_diff", {}))
        result, _, tools, _ = self.run_engine([same, same, same])

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(tools.calls, ["git_diff", "git_diff"])


if __name__ == "__main__":
    unittest.main()

