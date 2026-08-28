"""Bounded, redacted storage for command output and context artifacts."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .events import Redactor


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    output_id: str
    stored_chars: int
    original_chars: int
    truncated: bool


class ArtifactStore:
    _ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")

    def __init__(
        self,
        root: Path,
        redactor: Redactor,
        *,
        max_artifact_chars: int = 1_000_000,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._restrict_permissions(self.root, 0o700)
        self._redactor = redactor
        self._max_artifact_chars = max_artifact_chars

    def write_text(self, text: str) -> ArtifactRef:
        sanitized = self._redactor.text(text)
        original_chars = len(sanitized)
        truncated = original_chars > self._max_artifact_chars
        stored = sanitized[: self._max_artifact_chars]
        output_id = uuid.uuid4().hex
        path = self._path(output_id)
        path.write_text(stored, encoding="utf-8", newline="\n")
        self._restrict_permissions(path, 0o600)
        return ArtifactRef(output_id, len(stored), original_chars, truncated)

    def read_text(self, output_id: str, offset: int, limit: int) -> tuple[str, int]:
        if offset < 0 or limit <= 0:
            raise ValueError("offset must be non-negative and limit must be positive")
        text = self._path(output_id).read_text(encoding="utf-8")
        return text[offset : offset + limit], len(text)

    def search(self, output_id: str, pattern: str, max_results: int) -> list[dict[str, object]]:
        if not pattern:
            raise ValueError("pattern must not be empty")
        if max_results <= 0:
            raise ValueError("max_results must be positive")
        text = self._path(output_id).read_text(encoding="utf-8")
        results: list[dict[str, object]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            column = line.find(pattern)
            if column >= 0:
                results.append(
                    {"line": line_number, "column": column + 1, "text": line[:500]}
                )
                if len(results) >= max_results:
                    break
        return results

    def _path(self, output_id: str) -> Path:
        if not self._ID_PATTERN.fullmatch(output_id):
            raise ValueError("invalid output_id")
        return self.root / f"{output_id}.txt"

    @staticmethod
    def _restrict_permissions(path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except OSError:
            pass
