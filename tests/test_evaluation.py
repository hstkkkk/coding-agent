from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from coding_agent.domain import AssistantTurn, FinishRequest, RunOptions, ToolCall
from coding_agent.evaluation import EvaluationConfig, load_suite, run_evaluation
from coding_agent.events import Redactor
from coding_agent.model import ScriptedModelAdapter


class EvaluationHarnessTests(unittest.TestCase):
    def test_fixture_is_reset_and_checked_by_hidden_oracle(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        suite = load_suite(project_root / "evaluation" / "suite.json")

        def edit_from_context(request):
            hashes = re.findall(r'"sha256": "([a-f0-9]{64})"', request.user_prompt)
            if not hashes:
                raise AssertionError("file hash missing from context")
            return AssistantTurn(
                "fix the endpoint comparison",
                ToolCall(
                    "edit",
                    "edit_file",
                    {
                        "path": "intervals.py",
                        "old_text": "if start < current[1]:",
                        "new_text": "if start <= current[1]:",
                        "expected_sha256": hashes[-1],
                    },
                ),
            )

        def finish_from_context(request):
            ids = re.findall(r'"verification_id": "([a-f0-9]{32})"', request.user_prompt)
            if not ids:
                raise AssertionError("verification id missing from context")
            return AssistantTurn(
                "finish",
                FinishRequest("finish", "merged touching intervals", (ids[-1],)),
            )

        model = ScriptedModelAdapter(
            [
                AssistantTurn(
                    "reproduce",
                    ToolCall(
                        "baseline",
                        "run_command",
                        {
                            "program": "python",
                            "args": ["-m", "unittest", "discover", "-v"],
                            "purpose": "verify",
                        },
                    ),
                ),
                AssistantTurn("read", ToolCall("read", "read_file", {"path": "intervals.py"})),
                edit_from_context,
                AssistantTurn(
                    "verify",
                    ToolCall(
                        "verify",
                        "run_command",
                        {
                            "program": "python",
                            "args": ["-m", "unittest", "discover", "-v"],
                            "purpose": "verify",
                        },
                    ),
                ),
                finish_from_context,
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            report_path, report = run_evaluation(
                suite,
                EvaluationConfig(
                    model=model,
                    options=RunOptions(),
                    runs_root=Path(directory) / "runs",
                    redactor=Redactor(),
                    metadata={"model_name": "scripted"},
                ),
            )

            self.assertTrue(report_path.is_file())
            self.assertEqual(report["passed"], 1)
            self.assertEqual(report["false_successes"], 0)
            self.assertNotEqual(report["entries"][0]["baseline_exit_code"], 0)
            self.assertEqual(report["entries"][0]["oracle_exit_code"], 0)


if __name__ == "__main__":
    unittest.main()

