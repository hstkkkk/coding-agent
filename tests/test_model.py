from __future__ import annotations

import unittest

from coding_agent.domain import FinishRequest, ModelProtocolError, ModelRequest, RiskLevel, ToolDefinition
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


class StubAdapter(OpenAICompatibleAdapter):
    def __init__(self, response: dict[str, object]) -> None:
        super().__init__(api_key="unit-test-secret", model="test-model")
        self.response = response
        self.last_payload: dict[str, object] | None = None

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
        self.last_payload = payload
        return self.response


class ModelAdapterTests(unittest.TestCase):
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

    def test_rejects_multiple_actions(self) -> None:
        tool_call = {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"x.py"}'},
        }
        adapter = StubAdapter(
            {"choices": [{"message": {"content": "", "tool_calls": [tool_call, tool_call]}}]}
        )

        with self.assertRaises(ModelProtocolError):
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


if __name__ == "__main__":
    unittest.main()

