"""Sanitized event recording and human-readable presentation."""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .domain import EventSink, RunEvent, replace_unpaired_surrogates


class Redactor:
    """Best-effort defense against secrets entering logs or model context."""

    _ASSIGNMENT_PATTERN = re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|secret)"
        r"(\s*[:=]\s*)([^\s,;\"']+)"
    )
    _TOKEN_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b")

    def __init__(self, known_secrets: Iterable[str] = ()) -> None:
        self._known = tuple(
            sorted(
                {secret for secret in known_secrets if secret and len(secret) >= 4},
                key=len,
                reverse=True,
            )
        )

    def text(self, value: str) -> str:
        redacted = replace_unpaired_surrogates(value)
        for secret in self._known:
            redacted = redacted.replace(secret, "[REDACTED_SECRET]")
        redacted = self._TOKEN_PATTERN.sub("[REDACTED_TOKEN]", redacted)
        return self._ASSIGNMENT_PATTERN.sub(
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED_SECRET]",
            redacted,
        )

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, Path):
            return str(value)
        if is_dataclass(value):
            return self.value(asdict(value))
        if isinstance(value, dict):
            return {str(key): self.value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self.value(item) for item in value]
        return value


class JsonlEventSink(EventSink):
    def __init__(self, path: Path, redactor: Redactor) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(self.path.parent, 0o700)
        self.path.touch(exist_ok=True)
        _restrict_permissions(self.path, 0o600)
        self._redactor = redactor
        self._lock = threading.Lock()

    def emit(self, event: RunEvent) -> None:
        payload = self._redactor.value(event)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.write("\n")


class InMemoryEventSink(EventSink):
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def emit(self, event: RunEvent) -> None:
        self.events.append(event)


class ConsoleEventSink(EventSink):
    """Render only concise, non-sensitive progress information."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        self._redactor = redactor or Redactor()

    def emit(self, event: RunEvent) -> None:
        sanitized = self._redactor.value(event.data)
        assert isinstance(sanitized, dict)
        data = sanitized
        if event.kind == "model_action":
            parts = [
                _console_text(data.get("action", "unknown")),
                _console_text(data.get("detail", "")),
            ]
            repeated = data.get("repeated")
            if isinstance(repeated, int) and repeated > 1:
                parts.append(f"repeated {repeated}x")
            line = "[MODEL] " + " · ".join(part for part in parts if part)
            rationale = _console_text(data.get("rationale", ""), limit=180)
            if rationale:
                line += f" — {rationale}"
            print(line)
        elif event.kind == "tool_finished":
            detail = _console_text(data.get("detail", ""))
            detail_fragment = f" · {detail}" if detail else ""
            approval_wait = data.get("approval_wait_ms", 0)
            execution = data.get("execution_ms", data.get("duration_ms", 0))
            if "execution_ms" in data or (isinstance(approval_wait, int) and approval_wait > 0):
                timing = f"exec {execution} ms"
                if isinstance(approval_wait, int) and approval_wait > 0:
                    timing += f", approval {approval_wait} ms"
            else:
                timing = f"{data.get('duration_ms', 0)} ms"
            print(
                f"[TOOL] {_console_text(data.get('tool', 'unknown'))}{detail_fragment} -> "
                f"{_console_text(data.get('status', 'UNKNOWN'))} "
                f"({timing})"
            )
            recovery_id = data.get("recovery_output_id")
            if isinstance(recovery_id, str) and recovery_id:
                recovery_path = _console_text(data.get("recovery_path", "file"))
                print(
                    f"[RECOVERY] Before-image for {recovery_path} saved. Restore to "
                    "a new file with: coding-agent recover-file "
                    f"{event.run_id} {recovery_id} --output <path>"
                )
            screenshot_id = data.get("screenshot_id")
            if isinstance(screenshot_id, str) and screenshot_id:
                print(
                    "[BROWSER] Screenshot saved. Export it with: "
                    "coding-agent export-screenshot "
                    f"{event.run_id} {screenshot_id} --output preview.png"
                )
        elif event.kind == "verification_rejected":
            print(f"[VERIFY] rejected: {data.get('reason', '')}")
        elif event.kind == "answer_rejected":
            print(f"[ANSWER] rejected: {_console_text(data.get('reason', ''))}")
        elif event.kind == "tool_skipped":
            detail = _console_text(data.get("detail", ""))
            suffix = f" · {detail}" if detail else ""
            print(
                f"[TOOL] {_console_text(data.get('tool', 'unknown'))}{suffix} -> "
                f"SKIPPED · {_console_text(data.get('reason', ''))}"
            )
        elif event.kind == "tool_cached":
            detail = _console_text(data.get("detail", ""))
            suffix = f" · {detail}" if detail else ""
            print(
                f"[TOOL] {_console_text(data.get('tool', 'unknown'))}{suffix} -> "
                f"CACHED · {_console_text(data.get('reason', ''))}"
            )
        elif event.kind == "finalization_started":
            print(f"[FINALIZE] {_console_text(data.get('message', ''), limit=600)}")
        elif event.kind == "progress_required":
            print(f"[FOCUS] {_console_text(data.get('message', ''), limit=600)}")
        elif event.kind == "wrap_up_started":
            print(f"[WRAP-UP] {_console_text(data.get('message', ''), limit=600)}")
        elif event.kind == "terminal":
            if data.get("status") == "ANSWERED":
                print(f"[ANSWER] {_console_text(data.get('summary', ''), limit=4_000)}")
            else:
                print(f"[DONE] {data.get('status', 'UNKNOWN')}: {data.get('summary', '')}")
            warnings = data.get("warnings")
            if isinstance(warnings, list):
                for warning in warnings:
                    print(f"[WARNING] {_console_text(warning, limit=600)}")
        elif event.kind in {"retry", "warning"}:
            print(f"[{event.kind.upper()}] {_console_text(data.get('message', ''), limit=600)}")


class JsonConsoleEventSink(EventSink):
    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor

    def emit(self, event: RunEvent) -> None:
        print(
            json.dumps(
                self._redactor.value(event),
                ensure_ascii=False,
                sort_keys=True,
            )
        )


class CompositeEventSink(EventSink):
    def __init__(self, *sinks: EventSink) -> None:
        self._sinks = sinks

    def emit(self, event: RunEvent) -> None:
        for sink in self._sinks:
            sink.emit(event)


class EventEmitter:
    def __init__(self, run_id: str, sink: EventSink) -> None:
        self._run_id = run_id
        self._sink = sink
        self._sequence = 0

    def emit(self, kind: str, **data: Any) -> RunEvent:
        self._sequence += 1
        event = RunEvent(
            run_id=self._run_id,
            sequence=self._sequence,
            kind=kind,
            timestamp=datetime.now(timezone.utc).isoformat(),
            data=data,
        )
        self._sink.emit(event)
        return event


def _restrict_permissions(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        # Windows ACLs and some mounted filesystems do not implement POSIX modes.
        pass


def _console_text(value: Any, *, limit: int = 240) -> str:
    raw = " ".join(str(value).split())
    rendered: list[str] = []
    for character in raw[:limit]:
        if unicodedata.category(character).startswith("C"):
            rendered.append(f"\\u{ord(character):04x}")
        else:
            rendered.append(character)
    if len(raw) > limit:
        rendered.append("…")
    return "".join(rendered)
