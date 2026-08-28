"""Read-only Git observations used by tools and controller bookkeeping."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    available: bool
    head: str | None
    status: str
    changed_files: tuple[str, ...]


class GitInspector:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def snapshot(self) -> GitSnapshot:
        status = self._run("status", "--porcelain=v1", "--untracked-files=all")
        if status is None:
            return GitSnapshot(False, None, "", ())
        head = self._run("rev-parse", "HEAD")
        changed: list[str] = []
        for line in status.splitlines():
            if len(line) < 4:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            changed.append(path.replace("\\", "/"))
        return GitSnapshot(True, head.strip() if head else None, status, tuple(changed))

    def diff(self, *, staged: bool = False) -> str:
        args = ["diff", "--no-ext-diff", "--no-color"]
        if staged:
            args.append("--cached")
        result = self._run(*args)
        if result is None:
            raise ValueError("workspace is not a Git repository")
        return result

    def _run(self, *args: str) -> str | None:
        try:
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
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

