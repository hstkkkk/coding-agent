from __future__ import annotations

import io
import unittest

from coding_agent.domain import ApprovalRequest, RiskLevel
from coding_agent.policy import PromptApprovalAdapter


class PromptApprovalTests(unittest.TestCase):
    def test_initial_prompt_is_short_and_offers_details(self) -> None:
        marker = "long-inline-code-marker" * 300
        output = io.StringIO()
        adapter = PromptApprovalAdapter(
            input_stream=io.StringIO("y\n"),
            output_stream=output,
        )

        decision = adapter.request(self._request(marker))

        rendered = output.getvalue()
        self.assertTrue(decision.approved)
        self.assertIn("Approval required [EXECUTION]", rendered)
        self.assertIn("Request: program=node", rendered)
        self.assertIn("[d]etails", rendered)
        self.assertNotIn(marker, rendered)
        self.assertLess(len(rendered), 1_000)

    def test_details_show_full_redacted_arguments_then_reprompt(self) -> None:
        marker = "inspect-this-inline-code" * 30
        output = io.StringIO()
        adapter = PromptApprovalAdapter(
            input_stream=io.StringIO("d\ny\n"),
            output_stream=output,
            detail_width=60,
        )

        decision = adapter.request(self._request(marker))

        rendered = output.getvalue()
        self.assertTrue(decision.approved)
        self.assertIn("Full arguments", rendered)
        self.assertIn(marker[:20], rendered)
        self.assertIn("visual wrapping only", rendered)
        self.assertIn("a" * 64, rendered)
        self.assertGreaterEqual(rendered.count("Approve this exact digest?"), 2)

    def test_empty_answer_denies(self) -> None:
        adapter = PromptApprovalAdapter(
            input_stream=io.StringIO("\n"),
            output_stream=io.StringIO(),
        )

        decision = adapter.request(self._request("short"))

        self.assertFalse(decision.approved)

    @staticmethod
    def _request(inline_code: str) -> ApprovalRequest:
        return ApprovalRequest(
            action_id="action",
            tool_name="run_command",
            risk=RiskLevel.EXECUTION,
            summary=(
                "program=node · cwd=. · purpose=verify · 2 args · "
                f"inline code={len(inline_code)} chars"
            ),
            operation_digest="a" * 64,
            arguments={
                "program": "node",
                "args": ["-e", inline_code],
                "cwd": ".",
                "purpose": "verify",
            },
        )


if __name__ == "__main__":
    unittest.main()
