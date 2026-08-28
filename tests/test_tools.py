from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_agent.artifacts import ArtifactStore
from coding_agent.domain import ErrorCode, ToolCall, ToolStatus
from coding_agent.events import Redactor
from coding_agent.policy import FixedApprovalAdapter
from coding_agent.tools import LocalToolRuntime


class LocalToolRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)
        subprocess.run(["git", "config", "user.name", "Candidate"], cwd=self.workspace, check=True)
        subprocess.run(
            ["git", "config", "user.email", "candidate@example.invalid"],
            cwd=self.workspace,
            check=True,
        )
        (self.workspace / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        subprocess.run(["git", "add", "sample.txt"], cwd=self.workspace, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=self.workspace, check=True)
        self.redactor = Redactor(["unit-test-secret-value"])
        self.approvals = FixedApprovalAdapter(True)
        self.runtime = LocalToolRuntime(
            workspace=self.workspace,
            approvals=self.approvals,
            artifacts=ArtifactStore(root / "artifacts", self.redactor),
            redactor=self.redactor,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def execute(self, name: str, arguments: dict[str, object]):
        return self.runtime.execute(ToolCall("provider-id", name, arguments), "action-id")

    def test_read_and_hash_guarded_edit(self) -> None:
        read = self.execute("read_file", {"path": "sample.txt"})
        self.assertEqual(read.status, ToolStatus.COMPLETED)
        digest = read.data["sha256"]

        edited = self.execute(
            "edit_file",
            {
                "path": "sample.txt",
                "old_text": "beta",
                "new_text": "gamma",
                "expected_sha256": digest,
            },
        )
        stale = self.execute(
            "edit_file",
            {
                "path": "sample.txt",
                "old_text": "gamma",
                "new_text": "delta",
                "expected_sha256": digest,
            },
        )

        self.assertEqual(edited.status, ToolStatus.COMPLETED)
        self.assertTrue(edited.data["workspace_changed"])
        self.assertEqual(stale.status, ToolStatus.CONFLICT)
        self.assertEqual(stale.error_code, ErrorCode.TOOL_CONFLICT)
        self.assertEqual((self.workspace / "sample.txt").read_text(encoding="utf-8"), "alpha\ngamma\n")

    def test_rejects_path_escape_and_sensitive_file(self) -> None:
        (self.workspace / ".env").write_text("TOKEN=value", encoding="utf-8")

        escaped = self.execute("read_file", {"path": "../outside.txt"})
        sensitive = self.execute("read_file", {"path": ".env"})
        git_config = self.execute("read_file", {"path": ".git/config"})

        self.assertEqual(escaped.error_code, ErrorCode.POLICY_DENIED)
        self.assertEqual(sensitive.error_code, ErrorCode.POLICY_DENIED)
        self.assertEqual(git_config.error_code, ErrorCode.POLICY_DENIED)

    def test_command_nonzero_is_completed_and_does_not_inherit_api_key(self) -> None:
        arguments = {
            "program": "python",
            "args": [
                "-c",
                "import os,sys; print(os.getenv('OPENAI_API_KEY','missing')); sys.exit(1)",
            ],
            "purpose": "verify",
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-secret-value"}):
            result = self.execute("run_command", arguments)

        self.assertEqual(result.status, ToolStatus.COMPLETED)
        self.assertEqual(result.data["exit_code"], 1)
        self.assertEqual(result.error_code, ErrorCode.COMMAND_FAILED)
        self.assertIn("missing", result.data["stdout_preview"])
        self.assertNotIn("unit-test-secret-value", str(result.data))

    def test_execution_requires_approval(self) -> None:
        root = Path(self.temporary.name)
        denied = LocalToolRuntime(
            workspace=self.workspace,
            approvals=FixedApprovalAdapter(False),
            artifacts=ArtifactStore(root / "denied-artifacts", self.redactor),
            redactor=self.redactor,
        )
        result = denied.execute(
            ToolCall("provider", "run_command", {"program": "python", "args": ["-V"]}),
            "action",
        )

        self.assertEqual(result.status, ToolStatus.REJECTED)
        self.assertEqual(result.error_code, ErrorCode.APPROVAL_DENIED)

    def test_blocks_git_write_even_if_approved(self) -> None:
        result = self.execute("run_command", {"program": "git", "args": ["push"]})
        self.assertEqual(result.error_code, ErrorCode.POLICY_DENIED)
        self.assertEqual(len(self.approvals.requests), 0)

    def test_search_does_not_follow_file_symlink_outside_workspace(self) -> None:
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text("outside-secret-marker", encoding="utf-8")
        link = self.workspace / "linked.txt"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        result = self.execute("search_text", {"query": "outside-secret-marker"})

        self.assertEqual(result.status, ToolStatus.COMPLETED)
        self.assertEqual(result.data["matches"], [])

    def test_command_detects_change_to_an_already_dirty_file(self) -> None:
        (self.workspace / "sample.txt").write_text("already dirty\n", encoding="utf-8")
        result = self.execute(
            "run_command",
            {
                "program": "python",
                "args": [
                    "-c",
                    "from pathlib import Path; Path('sample.txt').write_text('changed again\\n')",
                ],
                "purpose": "operate",
            },
        )

        self.assertEqual(result.status, ToolStatus.COMPLETED)
        self.assertTrue(result.data["workspace_changed"])


if __name__ == "__main__":
    unittest.main()
