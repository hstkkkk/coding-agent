from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coding_agent.policy import PathPolicy
from coding_agent.tools.browser import BrowserRenderer
from coding_agent.tools.process import ProcessExecution


class _FakeProcessRunner:
    def __init__(self) -> None:
        self.executable: Path | None = None
        self.args: list[str] = []

    def run_executable(self, *, executable, args, cwd, timeout_seconds):
        self.executable = executable
        self.args = list(args)
        screenshot_argument = next(
            item for item in args if item.startswith("--screenshot=")
        )
        screenshot = Path(screenshot_argument.split("=", 1)[1])
        screenshot.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x03\x20\x00\x00\x02\x58"
            b"\x08\x06\x00\x00\x00"
        )
        return ProcessExecution(
            exit_code=0,
            stdout="<html><body><canvas></canvas></body></html>",
            stderr="",
            timed_out=False,
            duration_ms=25,
            output_truncated=False,
        )


class BrowserRendererTests(unittest.TestCase):
    def test_renders_local_html_with_isolated_profile_and_network_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "index.html").write_text(
                "<!doctype html><canvas></canvas>",
                encoding="utf-8",
            )
            browser = root / "browser.exe"
            browser.touch()
            runner = _FakeProcessRunner()
            renderer = BrowserRenderer(
                PathPolicy(workspace),
                root / "shots",
                process_runner=runner,
                browser_locator=lambda: browser,
            )

            result = renderer.render(
                path="index.html",
                viewport_width=800,
                viewport_height=600,
                wait_ms=250,
                timeout_seconds=10,
            )

            self.assertEqual(result.width, 800)
            self.assertEqual(result.height, 600)
            self.assertTrue(result.screenshot_path.is_file())
            self.assertEqual(result.dom, "<html><body><canvas></canvas></body></html>")
            self.assertTrue(any("host-resolver-rules" in item for item in runner.args))
            self.assertTrue(any(item.startswith("--user-data-dir=") for item in runner.args))
            self.assertIn((workspace / "index.html").as_uri(), runner.args)


if __name__ == "__main__":
    unittest.main()
