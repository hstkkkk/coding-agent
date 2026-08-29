from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from coding_agent.cli import build_parser, main


class CliTests(unittest.TestCase):
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

    def test_inspect_rejects_non_hex_run_id(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = main(["inspect-run", "../outside"])
        self.assertEqual(exit_code, 5)
        self.assertIn("run_id", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

