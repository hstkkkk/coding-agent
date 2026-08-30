from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from coding_agent.cli import build_parser, main
from coding_agent.domain import RunResult, RunStatus


class CliTests(unittest.TestCase):
    def test_bare_command_dispatches_to_tui(self) -> None:
        with patch("coding_agent.cli._tui", return_value=0) as tui:
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(tui.call_count, 1)

    def test_parser_exposes_explicit_tui_command(self) -> None:
        args = build_parser().parse_args(["tui"])

        self.assertEqual(args.command, "tui")
        self.assertEqual(args.workspace, Path.cwd())

    def test_parser_reads_explicit_thinking_mode_from_environment(self) -> None:
        with patch.dict(
            "os.environ",
            {"CODING_AGENT_THINKING": "disabled"},
            clear=True,
        ):
            args = build_parser().parse_args(
                ["eval", "--suite", "evaluation/suite.json"]
            )

        self.assertEqual(args.thinking, "disabled")

    def test_configuration_fails_before_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            stderr = io.StringIO()
            with patch.dict("os.environ", {}, clear=True), redirect_stderr(stderr):
                exit_code = main(["run", "fix it", "--workspace", str(workspace)])

            self.assertEqual(exit_code, 5)
            self.assertIn("CODING_AGENT_MODEL", stderr.getvalue())

    def test_invalid_configuration_does_not_initialize_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            stderr = io.StringIO()

            with patch.dict("os.environ", {}, clear=True), redirect_stderr(stderr):
                exit_code = main(["run", "fix it", "--workspace", str(workspace)])

            self.assertEqual(exit_code, 5)
            self.assertIn("CODING_AGENT_MODEL", stderr.getvalue())
            self.assertFalse((workspace / ".git").exists())

    def test_run_initializes_non_git_workspace_before_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as runs:
            workspace = Path(directory)
            (workspace / "app.py").write_text("print('hello')\n", encoding="utf-8")
            stdout = io.StringIO()
            environment = {
                "OPENAI_API_KEY": "test-api-value",
                "CODING_AGENT_MODEL": "test-model",
                "GIT_AUTHOR_NAME": "CLI Test",
                "GIT_AUTHOR_EMAIL": "cli-test@example.invalid",
                "GIT_COMMITTER_NAME": "CLI Test",
                "GIT_COMMITTER_EMAIL": "cli-test@example.invalid",
            }
            expected = RunResult(
                run_id="a" * 32,
                status=RunStatus.SUCCEEDED,
                summary="done",
                changed_files=(),
                verifications=(),
            )

            with (
                patch.dict("os.environ", environment),
                patch("coding_agent.cli._runs_root", return_value=Path(runs)),
                patch("coding_agent.cli.LocalAgentRunner.run", return_value=expected) as run,
                redirect_stdout(stdout),
            ):
                exit_code = main(["run", "fix it", "--workspace", str(workspace)])

            self.assertEqual(exit_code, 0)
            self.assertTrue((workspace / ".git").exists())
            self.assertIn("[SETUP] Initialized Git repository.", stdout.getvalue())
            self.assertEqual(run.call_count, 1)

    def test_inspect_rejects_non_hex_run_id(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = main(["inspect-run", "../outside"])
        self.assertEqual(exit_code, 5)
        self.assertIn("run_id", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

