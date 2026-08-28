"""Read-only Git observations used by tools and controller bookkeeping."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    available: bool
    head: str | None
    status: str
    changed_files: tuple[str, ...]
    fingerprint: str


class GitInspector:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def snapshot(self) -> GitSnapshot:
        status = self._run("status", "--porcelain=v1", "--untracked-files=all")
        if status is None:
            return GitSnapshot(False, None, "", (), "")
        head = self._run("rev-parse", "HEAD")
        changed: list[str] = []
        for line in status.splitlines():
            if len(line) < 4:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            changed.append(path.replace("\\", "/"))
        fingerprint = self._fingerprint(status, changed)
        return GitSnapshot(
            True,
            head.strip() if head else None,
            status,
            tuple(changed),
            fingerprint,
        )

    def diff(self, *, staged: bool = False) -> str:
        args = ["diff", "--no-ext-diff", "--no-color"]
        if staged:
            args.append("--cached")
        result = self._run(*args)
        if result is None:
            raise ValueError("workspace is not a Git repository")
        if not staged:
            untracked = [
                line[3:].replace("\\", "/")
                for line in (self._run("status", "--porcelain=v1", "--untracked-files=all") or "").splitlines()
                if line.startswith("?? ")
            ]
            if untracked:
                result += "\n" + "\n".join(f"Untracked file: {path}" for path in untracked) + "\n"
        return result

    def _fingerprint(self, status: str, changed: list[str]) -> str:
        digest = hashlib.sha256(status.encode("utf-8", errors="replace"))
        for args in (
            ("diff", "--no-ext-diff", "--no-color", "--binary"),
            ("diff", "--no-ext-diff", "--no-color", "--binary", "--cached"),
        ):
            output = self._run(*args) or ""
            digest.update(output.encode("utf-8", errors="replace"))
        for raw_path in sorted(changed):
            path = self.workspace / raw_path
            digest.update(raw_path.encode("utf-8", errors="replace"))
            try:
                if path.is_symlink():
                    digest.update(os.readlink(path).encode("utf-8", errors="replace"))
                elif path.is_file():
                    with path.open("rb") as handle:
                        remaining = 5_000_000
                        while remaining > 0:
                            chunk = handle.read(min(65_536, remaining))
                            if not chunk:
                                break
                            digest.update(chunk)
                            remaining -= len(chunk)
                    digest.update(str(path.stat().st_size).encode("ascii"))
            except OSError:
                digest.update(b"[unreadable]")
        return digest.hexdigest()

    def _run(self, *args: str) -> str | None:
        try:
            environment = {
                key: value
                for key, value in os.environ.items()
                if key.upper()
                in {
                    "COMSPEC",
                    "LANG",
                    "LC_ALL",
                    "PATH",
                    "PATHEXT",
                    "SYSTEMROOT",
                    "TEMP",
                    "TMP",
                    "WINDIR",
                }
            }
            result = subprocess.run(
                ["git", *args],
                cwd=self.workspace,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                shell=False,
                check=False,
                env=environment,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout
