from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from coding_agent.artifacts import ArtifactStore
from coding_agent.domain import AssistantTurn, FinishRequest, RunStatus, TaskRequest, ToolCall
from coding_agent.engine import AgentEngine
from coding_agent.events import InMemoryEventSink, Redactor
from coding_agent.model import ScriptedModelAdapter
from coding_agent.policy import FixedApprovalAdapter
from coding_agent.tools import LocalToolRuntime


class FullLoopIntegrationTests(unittest.TestCase):
    def test_real_tools_fix_and_verify_a_temporary_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
            (workspace / "calculator.py").write_text(
                "def add(left, right):\n    return left - right\n",
                encoding="utf-8",
            )
            (workspace / "test_calculator.py").write_text(
                "import unittest\n\n"
                "from calculator import add\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Candidate"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.email", "candidate@example.invalid"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=workspace, check=True)

            def edit_from_context(request):
                hashes = re.findall(r'"sha256": "([a-f0-9]{64})"', request.user_prompt)
                if not hashes:
                    raise AssertionError("read_file hash missing from context")
                return AssistantTurn(
                    "replace the incorrect operator",
                    ToolCall(
                        "edit",
                        "edit_file",
                        {
                            "path": "calculator.py",
                            "old_text": "return left - right",
                            "new_text": "return left + right",
                            "expected_sha256": hashes[-1],
                        },
                    ),
                )

            def finish_from_context(request):
                verification_ids = re.findall(
                    r'"verification_id": "([a-f0-9]{32})"', request.user_prompt
                )
                if not verification_ids:
                    raise AssertionError("verification id missing from context")
                return AssistantTurn(
                    "cite the passing verification",
                    FinishRequest("finish", "fixed addition", (verification_ids[-1],)),
                )

            model = ScriptedModelAdapter(
                [
                    AssistantTurn(
                        "reproduce the failure",
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
                    AssistantTurn(
                        "inspect the implementation",
                        ToolCall("read", "read_file", {"path": "calculator.py"}),
                    ),
                    edit_from_context,
                    AssistantTurn(
                        "verify the current workspace",
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
            redactor = Redactor()
            runtime = LocalToolRuntime(
                workspace=workspace,
                approvals=FixedApprovalAdapter(True),
                artifacts=ArtifactStore(root / "artifacts", redactor),
                redactor=redactor,
            )
            events = InMemoryEventSink()
            result = AgentEngine(model=model, tools=runtime, events=events).run(
                TaskRequest("fix add so the existing test passes", workspace)
            )

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertIn("return left + right", (workspace / "calculator.py").read_text())
            self.assertEqual([item.passed for item in result.verifications], [False, True])
            self.assertTrue(any(event.kind == "terminal" for event in events.events))


if __name__ == "__main__":
    unittest.main()

