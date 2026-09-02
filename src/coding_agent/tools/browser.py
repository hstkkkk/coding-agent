"""Isolated headless-browser rendering for local HTML verification."""

from __future__ import annotations

import os
import shutil
import struct
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..policy import PathPolicy
from .filesystem import ToolInputError
from .process import ProcessExecution, ProcessRunner


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024


class BrowserProcessPort(Protocol):
    def run_executable(
        self,
        *,
        executable: Path,
        args: list[str],
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessExecution: ...


@dataclass(frozen=True, slots=True)
class BrowserRender:
    screenshot_id: str
    screenshot_path: Path
    screenshot_bytes: int
    width: int
    height: int
    dom: str
    duration_ms: int
    output_truncated: bool


class BrowserRenderer:
    """Render one workspace HTML file with a disposable browser profile."""

    def __init__(
        self,
        path_policy: PathPolicy,
        output_root: Path,
        *,
        process_runner: BrowserProcessPort | None = None,
        browser_locator: Callable[[], Path | None] | None = None,
    ) -> None:
        self.path_policy = path_policy
        self.output_root = output_root.resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(self.output_root, 0o700)
        self.process_runner = process_runner or ProcessRunner()
        self.browser_locator = browser_locator or find_browser_executable

    def render(
        self,
        *,
        path: str,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        wait_ms: int = 500,
        timeout_seconds: int = 30,
    ) -> BrowserRender:
        target = self.path_policy.resolve(path)
        if not target.is_file() or target.suffix.lower() not in {".html", ".htm"}:
            raise ToolInputError("browser_check path must be an HTML file")
        if not 320 <= viewport_width <= 3840 or not 240 <= viewport_height <= 2160:
            raise ToolInputError("browser viewport is outside the supported range")
        if not 0 <= wait_ms <= 5_000:
            raise ToolInputError("browser wait_ms must be between 0 and 5000")
        if not 1 <= timeout_seconds <= 120:
            raise ToolInputError("browser timeout must be between 1 and 120 seconds")

        executable = self.browser_locator()
        if executable is None or not executable.is_file():
            raise ToolInputError(
                "no supported headless browser was found (Edge, Chrome, or Chromium)"
            )

        screenshot_id = uuid.uuid4().hex
        screenshot = self.output_root / f"{screenshot_id}.png"
        with tempfile.TemporaryDirectory(prefix="coding-agent-browser-") as profile:
            args = [
                "--headless=new",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--no-first-run",
                "--no-default-browser-check",
                "--metrics-recording-only",
                "--force-device-scale-factor=1",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                f"--user-data-dir={profile}",
                f"--window-size={viewport_width},{viewport_height}",
                f"--virtual-time-budget={max(wait_ms, 1)}",
                f"--screenshot={screenshot}",
                "--dump-dom",
                target.as_uri(),
            ]
            execution = self.process_runner.run_executable(
                executable=executable,
                args=args,
                cwd=self.path_policy.workspace,
                timeout_seconds=timeout_seconds,
            )

        if execution.timed_out:
            screenshot.unlink(missing_ok=True)
            raise ToolInputError("headless browser timed out")
        if execution.exit_code != 0:
            screenshot.unlink(missing_ok=True)
            message = " ".join(execution.stderr.split())[:500]
            raise ToolInputError(
                "headless browser failed" + (f": {message}" if message else "")
            )
        width, height, screenshot_bytes = _validate_screenshot(screenshot)
        if width != viewport_width or height != viewport_height:
            screenshot.unlink(missing_ok=True)
            raise ToolInputError("headless browser returned an unexpected screenshot size")
        _restrict_permissions(screenshot, 0o600)
        return BrowserRender(
            screenshot_id=screenshot_id,
            screenshot_path=screenshot,
            screenshot_bytes=screenshot_bytes,
            width=width,
            height=height,
            dom=execution.stdout,
            duration_ms=execution.duration_ms,
            output_truncated=execution.output_truncated,
        )


def find_browser_executable() -> Path | None:
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        resolved = shutil.which(name)
        if resolved:
            return Path(resolved).resolve()

    candidates: list[Path] = []
    if os.name == "nt":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if not base:
                continue
            root = Path(base)
            candidates.extend(
                [
                    root / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    root / "Google" / "Chrome" / "Application" / "chrome.exe",
                ]
            )
    elif sys_platform_is_macos():
        candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            ]
        )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def sys_platform_is_macos() -> bool:
    import sys

    return sys.platform == "darwin"


def _validate_screenshot(path: Path) -> tuple[int, int, int]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > _MAX_SCREENSHOT_BYTES:
            raise ToolInputError("headless browser screenshot has an invalid size")
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError as exc:
        raise ToolInputError("headless browser did not produce a screenshot") from exc
    if len(header) < 24 or header[:8] != _PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ToolInputError("headless browser screenshot is not a valid PNG")
    width, height = struct.unpack(">II", header[16:24])
    return width, height, size


def _restrict_permissions(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass
