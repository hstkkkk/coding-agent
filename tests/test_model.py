from __future__ import annotations

import unittest

from coding_agent.domain import (
    AnswerRequest,
    FinishRequest,
    ModelProtocolError,
    ModelRequest,
    RiskLevel,
    ToolCall,
    ToolDefinition,
)
from coding_agent.model import OpenAICompatibleAdapter


READ_TOOL = ToolDefinition(
    name="read_file",
    description="read",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    risk=RiskLevel.READ_ONLY,
)
FINISH_TOOL = ToolDefinition(
    name="finish",
    description="finish",
    input_schema={
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "verification_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "verification_ids"],
    },
    risk=RiskLevel.READ_ONLY,
)
ANSWER_TOOL = ToolDefinition(
    name="respond",
    description="answer without changing the workspace",
    input_schema={
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    },
    risk=RiskLevel.READ_ONLY,
)


class StubAdapter(OpenAICompatibleAdapter):
    def __init__(
        self,
        response: dict[str, object],
        *,
        thinking: str | None = None,
    ) -> None:
        super().__init__(
            api_key="unit-test-secret",
            model="test-model",
            thinking=thinking,
        )
        self.response = response
        self.last_payload: dict[str, object] | None = None

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
        self.last_payload = payload
        return self.response


class ModelAdapterTests(unittest.TestCase):
    def test_includes_explicit_thinking_mode_in_request_payload(self) -> None:
        adapter = StubAdapter(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"x.py"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            thinking="disabled",
        )

        adapter.complete(ModelRequest("system", "task", (READ_TOOL,)))

        assert adapter.last_payload is not None
        self.assertEqual(adapter.last_payload["thinking"], {"type": "disabled"})
        self.assertEqual(adapter.last_payload["tool_choice"], "required")

    def test_thinking_enabled_uses_compatible_automatic_tool_choice(self) -> None:
        adapter = StubAdapter(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "respond",
                                        "arguments": '{"message":"hello"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            thinking="enabled",
        )

        adapter.complete(ModelRequest("system", "task", (ANSWER_TOOL,)))

        assert adapter.last_payload is not None
        self.assertEqual(adapter.last_payload["thinking"], {"type": "enabled"})
        self.assertEqual(adapter.last_payload["tool_choice"], "auto")

    def test_normalizes_finish_control_action(self) -> None:
        adapter = StubAdapter(
            {
                "choices": [
                    {
                        "message": {
                            "content": "Evidence is fresh.",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "finish",
                                        "arguments": '{"summary":"fixed","verification_ids":["v1"]}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )
        request = ModelRequest("system", "task", (READ_TOOL, FINISH_TOOL))

        turn = adapter.complete(request)

        self.assertIsInstance(turn.action, FinishRequest)
        assert isinstance(turn.action, FinishRequest)
        self.assertEqual(turn.action.verification_ids, ("v1",))
        self.assertEqual(turn.rationale, "Evidence is fresh.")
        self.assertEqual(adapter.last_payload["tool_choice"], "required")

    def test_normalizes_respond_control_action(self) -> None:
        adapter = StubAdapter(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-answer",
                                    "function": {
                                        "name": "respond",
                                        "arguments": '{"message":"I am a coding agent."}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )

        turn = adapter.complete(ModelRequest("system", "task", (ANSWER_TOOL,)))

        self.assertIsInstance(turn.action, AnswerRequest)
        assert isinstance(turn.action, AnswerRequest)
        self.assertEqual(turn.action.message, "I am a coding agent.")

    def test_rejects_unknown_respond_argument(self) -> None:
        adapter = StubAdapter(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-answer",
                                    "function": {
                                        "name": "respond",
                                        "arguments": '{"message":"hello","force":true}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )

        with self.assertRaises(ModelProtocolError):
            adapter.complete(ModelRequest("system", "task", (ANSWER_TOOL,)))

    def test_serializes_multiple_actions_by_normalizing_only_the_first(self) -> None:
        first_call = {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"x.py"}'},
        }
        second_call = {
            "id": "call-2",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"y.py"}'},
        }
        adapter = StubAdapter(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [first_call, second_call],
                        }
                    }
                ]
            }
        )

        turn = adapter.complete(ModelRequest("system", "task", (READ_TOOL,)))

        self.assertIsInstance(turn.action, ToolCall)
        assert isinstance(turn.action, ToolCall)
        self.assertEqual(turn.action.arguments, {"path": "x.py"})
        self.assertEqual(turn.proposed_action_count, 2)

    def test_rejects_a_response_without_any_action(self) -> None:
        adapter = StubAdapter(
            {"choices": [{"message": {"content": "", "tool_calls": []}}]}
        )

        with self.assertRaisesRegex(ModelProtocolError, "at least one tool call"):
            adapter.complete(ModelRequest("system", "task", (READ_TOOL,)))

    def test_rejects_unknown_tool(self) -> None:
        adapter = StubAdapter(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {"name": "steal_secret", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            }
        )

        with self.assertRaises(ModelProtocolError):
            adapter.complete(ModelRequest("system", "task", (READ_TOOL,)))

    def test_rejects_unknown_finish_argument(self) -> None:
        adapter = StubAdapter(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "finish",
                                        "arguments": '{"summary":"done","verification_ids":[],"force":true}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )

        with self.assertRaises(ModelProtocolError):
            adapter.complete(ModelRequest("system", "task", (FINISH_TOOL,)))


if __name__ == "__main__":
    unittest.main()
