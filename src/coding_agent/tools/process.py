"""Structured, bounded subprocess execution without a shell."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProcessExecution:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    output_truncated: bool


class ProcessRunner:
    _ENV_ALLOWLIST = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "VIRTUAL_ENV",
        "WINDIR",
    }

    def __init__(self, *, max_capture_bytes: int = 1_000_000) -> None:
        self.max_capture_bytes = max_capture_bytes

    def run(
        self,
        *,
        program: str,
        args: list[str],
        cwd: Path,
        timeout_seconds: int,
    ) -> ProcessExecution:
        if not program or any(separator in program for separator in ("/", "\\")):
            raise ValueError("program must be a PATH-resolved executable name")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        command = [program, *args]
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in self._ENV_ALLOWLIST
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"

        creationflags = 0
        popen_options: dict[str, object] = {}
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_options["start_new_session"] = True

        started = time.monotonic()
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                env=environment,
                shell=False,
                creationflags=creationflags,
                **popen_options,
            )
            timed_out = False
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_tree(process)
                self._ensure_stopped(process)
            except KeyboardInterrupt:
                self._terminate_tree(process)
                self._ensure_stopped(process)
                raise

            stdout, stdout_truncated = self._read_bounded(stdout_file)
            stderr, stderr_truncated = self._read_bounded(stderr_file)

        return ProcessExecution(
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=int((time.monotonic() - started) * 1000),
            output_truncated=stdout_truncated or stderr_truncated,
        )

    def _read_bounded(self, handle: object) -> tuple[str, bool]:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(0)
        if size <= self.max_capture_bytes:
            data = handle.read()
            return data.decode("utf-8", errors="replace"), False
        half = self.max_capture_bytes // 2
        head = handle.read(half)
        handle.seek(-half, os.SEEK_END)
        tail = handle.read(half)
        marker = b"\n...[output truncated]...\n"
        return (head + marker + tail).decode("utf-8", errors="replace"), True

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                shell=False,
                timeout=10,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @staticmethod
    def _ensure_stopped(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
