from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from coding_agent.domain import AssistantTurn, BlockedRequest, RunOptions, RunStatus
from coding_agent.local_runner import LocalAgentRunner, LocalRunSettings
from coding_agent.model import ScriptedModelAdapter


class LocalAgentRunnerTests(unittest.TestCase):
    def test_each_call_gets_isolated_run_state_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as runs:
            workspace = Path(directory)
            self._git(workspace, "init", "-q")
            model = ScriptedModelAdapter(
                [
                    AssistantTurn("blocked", BlockedRequest("first", "need input")),
                    AssistantTurn("blocked", BlockedRequest("second", "need input")),
                ]
            )
            runner = LocalAgentRunner(
                LocalRunSettings(
                    workspace=workspace,
                    api_key="test-api-value",
                    model_name="test-model",
                    base_url="https://example.invalid/v1",
                    thinking=None,
                    options=RunOptions(max_model_turns=2),
                    approval_mode="deny",
                    allowed_programs=frozenset(),
                    runs_root=Path(runs),
                    run_metadata={"mode": "test"},
                ),
                model=model,
            )

            with redirect_stdout(io.StringIO()):
                first = runner.run("first task")
                second = runner.run("second task")

            self.assertEqual(first.status, RunStatus.BLOCKED)
            self.assertEqual(second.status, RunStatus.BLOCKED)
            self.assertNotEqual(first.run_id, second.run_id)
            self.assertTrue((Path(runs) / first.run_id / "events.jsonl").is_file())
            self.assertTrue((Path(runs) / second.run_id / "events.jsonl").is_file())
            self.assertEqual(len(model.requests), 2)

    @staticmethod
    def _git(workspace: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    unittest.main()
