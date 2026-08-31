from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from coding_agent.cli import (
    EXIT_CODES,
    _runs_root,
    _validate_agent_configuration,
    build_parser,
    main,
)
from coding_agent.domain import RunResult, RunStatus
from coding_agent.settings import SettingsError, UserSettings


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings_loader = patch(
            "coding_agent.cli.load_user_settings",
            return_value=UserSettings(),
        )
        self.load_user_settings = self.settings_loader.start()
        self.addCleanup(self.settings_loader.stop)

    def test_bare_command_dispatches_to_tui(self) -> None:
        with patch("coding_agent.cli._tui", return_value=0) as tui:
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(tui.call_count, 1)

    def test_answered_run_has_a_successful_process_exit(self) -> None:
        self.assertEqual(EXIT_CODES[RunStatus.ANSWERED], 0)

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

    def test_parser_uses_settings_before_environment(self) -> None:
        settings = UserSettings(
            api_key="settings-secret",
            api_key_env="SETTINGS_API_KEY",
            model="settings-model",
            base_url="https://settings.example/v1",
            thinking=None,
            max_turns=44,
            max_seconds=1000,
            command_timeout=130,
            approval_mode="deny",
            allow_programs=("python",),
            provided_fields=frozenset(
                {
                    "api_key",
                    "api_key_env",
                    "model",
                    "base_url",
                    "thinking",
                    "max_turns",
                    "max_seconds",
                    "command_timeout",
                    "approval_mode",
                    "allow_programs",
                }
            ),
        )
        environment = {
            "SETTINGS_API_KEY": "environment-secret",
            "CODING_AGENT_MODEL": "environment-model",
            "CODING_AGENT_BASE_URL": "https://environment.example/v1",
            "CODING_AGENT_THINKING": "enabled",
        }
        with patch.dict("os.environ", environment, clear=True):
            args = build_parser(settings).parse_args(["tui"])

        self.assertEqual(args.model, "settings-model")
        self.assertEqual(args.base_url, "https://settings.example/v1")
        self.assertIsNone(args.thinking)
        self.assertEqual(args.max_turns, 44)
        self.assertEqual(args.max_seconds, 1000)
        self.assertEqual(args.command_timeout, 130)
        self.assertEqual(args.approval_mode, "deny")
        self.assertEqual(args.configured_allow_programs, ("python",))
        self.assertEqual(_validate_agent_configuration(args), "settings-secret")
        self.assertNotIn("settings-secret", repr(args))

    def test_command_line_overrides_settings(self) -> None:
        settings = UserSettings(
            model="settings-model",
            max_turns=44,
            allow_programs=("python",),
        )

        args = build_parser(settings).parse_args(
            [
                "tui",
                "--model",
                "cli-model",
                "--max-turns",
                "55",
                "--allow-program",
                "node",
            ]
        )

        self.assertEqual(args.model, "cli-model")
        self.assertEqual(args.max_turns, 55)
        self.assertEqual(args.allow_program, ["node"])

    def test_settings_run_directory_overrides_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configured = Path(directory) / "settings-runs"
            with patch.dict(
                "os.environ",
                {"CODING_AGENT_RUNS_DIR": str(Path(directory) / "environment-runs")},
                clear=True,
            ):
                result = _runs_root(configured)

        self.assertEqual(result, configured.resolve())

    def test_help_does_not_load_malformed_settings(self) -> None:
        self.load_user_settings.side_effect = SettingsError("broken settings")
        self.load_user_settings.reset_mock()

        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        self.load_user_settings.assert_not_called()

    def test_malformed_settings_returns_configuration_error(self) -> None:
        self.load_user_settings.side_effect = SettingsError("settings file is invalid")
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            exit_code = main([])

        self.assertEqual(exit_code, 5)
        self.assertIn("settings file is invalid", stderr.getvalue())

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

