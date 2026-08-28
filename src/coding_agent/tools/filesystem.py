"""Workspace-confined filesystem tool implementation."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from ..policy import PathPolicy


class ToolInputError(ValueError):
    pass


class EditConflict(ValueError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FileTools:
    def __init__(
        self,
        policy: PathPolicy,
        *,
        max_read_chars: int = 100_000,
        max_search_file_bytes: int = 1_000_000,
    ) -> None:
        self.policy = policy
        self.max_read_chars = max_read_chars
        self.max_search_file_bytes = max_search_file_bytes

    def list_directory(
        self,
        *,
        path: str = ".",
        recursive: bool = False,
        max_entries: int = 500,
    ) -> dict[str, Any]:
        target = self.policy.resolve(path)
        if not target.is_dir():
            raise ToolInputError("path is not a directory")
        if max_entries <= 0 or max_entries > 2_000:
            raise ToolInputError("max_entries must be between 1 and 2000")

        iterator = target.rglob("*") if recursive else target.iterdir()
        entries: list[dict[str, Any]] = []
        for item in iterator:
            relative = item.relative_to(self.policy.workspace)
            if ".git" in relative.parts:
                continue
            entries.append(
                {
                    "path": relative.as_posix(),
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None,
                }
            )
            if len(entries) >= max_entries:
                break
        entries.sort(key=lambda item: str(item["path"]))
        return {"entries": entries, "truncated": len(entries) >= max_entries}

    def read_file(
        self,
        *,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        target = self.policy.resolve(path)
        if not target.is_file():
            raise ToolInputError("path is not a file")
        if start_line < 1:
            raise ToolInputError("start_line must be at least 1")
        if end_line is not None and end_line < start_line:
            raise ToolInputError("end_line must not be before start_line")

        try:
            with target.open("r", encoding="utf-8", newline="") as handle:
                text = handle.read(self.max_read_chars + 1)
        except UnicodeDecodeError as exc:
            raise ToolInputError("only UTF-8 text files are supported") from exc

        truncated_by_size = len(text) > self.max_read_chars
        text = text[: self.max_read_chars]
        lines = text.splitlines(keepends=True)
        final_line = end_line if end_line is not None else len(lines)
        selected = "".join(lines[start_line - 1 : final_line])
        return {
            "path": target.relative_to(self.policy.workspace).as_posix(),
            "content": selected,
            "sha256": file_sha256(target),
            "start_line": start_line,
            "end_line": min(final_line, len(lines)),
            "observed_lines": len(lines),
            "truncated": truncated_by_size,
        }

    def search_text(
        self,
        *,
        query: str,
        path: str = ".",
        glob: str = "*",
        max_results: int = 100,
    ) -> dict[str, Any]:
        if not query:
            raise ToolInputError("query must not be empty")
        if max_results <= 0 or max_results > 500:
            raise ToolInputError("max_results must be between 1 and 500")
        target = self.policy.resolve(path)
        candidates = [target] if target.is_file() else target.rglob("*")
        results: list[dict[str, Any]] = []

        for file_path in candidates:
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(self.policy.workspace)
            if ".git" in relative.parts or not fnmatch.fnmatch(relative.name, glob):
                continue
            try:
                if file_path.stat().st_size > self.max_search_file_bytes:
                    continue
                with file_path.open("r", encoding="utf-8", errors="strict") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        column = line.find(query)
                        if column >= 0:
                            results.append(
                                {
                                    "path": relative.as_posix(),
                                    "line": line_number,
                                    "column": column + 1,
                                    "text": line.rstrip("\r\n")[:500],
                                }
                            )
                            if len(results) >= max_results:
                                return {"matches": results, "truncated": True}
            except (UnicodeDecodeError, OSError):
                continue
        return {"matches": results, "truncated": False}

    def edit_file(
        self,
        *,
        path: str,
        old_text: str,
        new_text: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        if not old_text:
            raise ToolInputError("old_text must not be empty")
        target = self.policy.resolve(path, write=True)
        if not target.is_file():
            raise ToolInputError("path is not a file")
        actual_hash = file_sha256(target)
        if actual_hash != expected_sha256:
            raise EditConflict(f"file hash changed; current sha256 is {actual_hash}")
        with target.open("r", encoding="utf-8", newline="") as handle:
            original = handle.read()
        matches = original.count(old_text)
        if matches != 1:
            raise EditConflict(f"old_text matched {matches} locations; expected exactly one")
        updated = original.replace(old_text, new_text, 1)
        self._atomic_replace(target, updated)
        return {
            "path": target.relative_to(self.policy.workspace).as_posix(),
            "old_sha256": actual_hash,
            "new_sha256": file_sha256(target),
            "changed_files": [target.relative_to(self.policy.workspace).as_posix()],
            "workspace_changed": True,
        }

    def create_file(self, *, path: str, content: str) -> dict[str, Any]:
        target = self.policy.resolve(path, write=True)
        if target.exists():
            raise EditConflict("target already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
        except FileExistsError as exc:
            raise EditConflict("target already exists") from exc
        relative = target.relative_to(self.policy.workspace).as_posix()
        return {
            "path": relative,
            "sha256": file_sha256(target),
            "changed_files": [relative],
            "workspace_changed": True,
        }

    def delete_file(self, *, path: str, expected_sha256: str) -> dict[str, Any]:
        target = self.policy.resolve(path, write=True)
        if not target.is_file():
            raise ToolInputError("path is not a file")
        actual_hash = file_sha256(target)
        if actual_hash != expected_sha256:
            raise EditConflict(f"file hash changed; current sha256 is {actual_hash}")
        relative = target.relative_to(self.policy.workspace).as_posix()
        target.unlink()
        return {
            "path": relative,
            "deleted_sha256": actual_hash,
            "changed_files": [relative],
            "workspace_changed": True,
        }

    @staticmethod
    def _atomic_replace(target: Path, text: str) -> None:
        mode = target.stat().st_mode
        descriptor, temporary_name = tempfile.mkstemp(prefix=".agent-edit-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

