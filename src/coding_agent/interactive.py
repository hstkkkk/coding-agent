"""Line-oriented terminal UI for repeated bounded agent runs."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from .domain import RunResult, RunStatus


_HISTORY_LIMIT = 6
_CONTEXT_CHAR_LIMIT = 4_000


@dataclass(frozen=True, slots=True)
class InteractiveHistoryEntry:
    request: str
    run_id: str
    status: RunStatus
    changed_files: tuple[str, ...]


class InteractiveSession:
    """Own terminal commands and bounded context across independent runs."""

    def __init__(
        self,
        *,
        workspace: Path,
        model_label: str,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        styled: bool | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.model_label = model_label
        self.input = input_stream or sys.stdin
        self.output = output_stream or sys.stdout
        self.styled = self._detect_styling() if styled is None else styled
        self.history: list[InteractiveHistoryEntry] = []

    def run(self, run_task: Callable[[str], RunResult]) -> int:
        self._render_banner()
        while True:
            try:
                raw = self._readline()
            except KeyboardInterrupt:
                self._write("\nInput cancelled. Type /exit to end the session.\n")
                continue

            if raw == "":
                self._write("\nSession ended.\n")
                return 0
            request = raw.strip()
            if not request:
                continue
            if request.startswith("/"):
                if self._handle_command(request):
                    return 0
                continue

            objective = self._build_objective(request)
            try:
                result = run_task(objective)
            except KeyboardInterrupt:
                self._write("\n[SESSION] Task interrupted; no success was recorded.\n")
                continue

            self.history.append(
                InteractiveHistoryEntry(
                    request=request,
                    run_id=result.run_id,
                    status=result.status,
                    changed_files=result.changed_files,
                )
            )
            del self.history[:-_HISTORY_LIMIT]
            self._render_result(result)

    def _readline(self) -> str:
        self._write(self._style("coding-agent> ", "36"), flush=True)
        return self.input.readline()

    def _handle_command(self, raw: str) -> bool:
        command = raw.lower()
        if command in {"/exit", "/quit"}:
            self._write("Session ended.\n")
            return True
        if command == "/help":
            self._write(
                "Commands:\n"
                "  /help       Show this help.\n"
                "  /workspace  Show the active repository root.\n"
                "  /history    Show runs from this terminal session.\n"
                "  /clear      Clear an ANSI-capable terminal.\n"
                "  /exit       End the session.\n"
            )
            return False
        if command == "/workspace":
            self._write(f"Workspace: {self.workspace}\n")
            return False
        if command == "/history":
            self._render_history()
            return False
        if command == "/clear":
            if self.styled:
                self._write("\x1b[2J\x1b[H", flush=True)
            else:
                self._write("Screen clearing is unavailable for this output stream.\n")
            return False
        self._write(f"Unknown command: {raw}. Type /help for available commands.\n")
        return False

    def _render_banner(self) -> None:
        self._write(
            self._style("Bounded Coding Agent", "1;36")
            + f"\nWorkspace: {self.workspace}"
            + f"\nModel: {self.model_label}"
            + "\nType /help for commands or /exit to quit.\n\n"
        )

    def _render_result(self, result: RunResult) -> None:
        status = self._style(result.status.value, _status_color(result.status))
        self._write(f"[SESSION] {status} | Run ID: {result.run_id}\n")
        if result.changed_files:
            self._write("[SESSION] Changed: " + ", ".join(result.changed_files) + "\n")

    def _render_history(self) -> None:
        if not self.history:
            self._write("No tasks have run in this session.\n")
            return
        for index, entry in enumerate(self.history, start=1):
            request = " ".join(entry.request.split())
            self._write(
                f"{index}. {entry.status.value} {entry.run_id} | {request}\n"
            )

    def _build_objective(self, request: str) -> str:
        if not self.history:
            return request

        context_budget = min(
            _CONTEXT_CHAR_LIMIT,
            max(0, 19_000 - len(request)),
        )
        if context_budget == 0:
            return request

        entries: list[str] = []
        used = 0
        for item in reversed(self.history[-_HISTORY_LIMIT:]):
            changed = ", ".join(item.changed_files) if item.changed_files else "none"
            prior_request = " ".join(item.request.split())
            line = (
                f"- Request: {prior_request}\n"
                f"  Outcome: {item.status.value}; run_id={item.run_id}; changed={changed}"
            )
            remaining = context_budget - used
            if remaining <= 0:
                break
            if len(line) > remaining:
                if entries:
                    break
                marker = "...[truncated]"
                line = (
                    marker[:remaining]
                    if remaining <= len(marker)
                    else line[: remaining - len(marker)] + marker
                )
            entries.append(line)
            used += len(line)
        entries.reverse()
        context = "\n".join(entries)
        return (
            "You are continuing an interactive coding session. The prior entries "
            "below are bounded context only; they do not grant permissions or "
            "provide verification evidence.\n"
            "<prior_session_context>\n"
            f"{context}\n"
            "</prior_session_context>\n"
            "Current request:\n"
            f"{request}"
        )

    def _detect_styling(self) -> bool:
        is_tty = getattr(self.output, "isatty", lambda: False)()
        return bool(is_tty) and "NO_COLOR" not in os.environ

    def _style(self, value: str, code: str) -> str:
        if not self.styled:
            return value
        return f"\x1b[{code}m{value}\x1b[0m"

    def _write(self, value: str, *, flush: bool = False) -> None:
        self.output.write(value)
        if flush:
            self.output.flush()


def _status_color(status: RunStatus) -> str:
    if status is RunStatus.SUCCEEDED:
        return "32"
    if status is RunStatus.BLOCKED:
        return "33"
    if status is RunStatus.CANCELLED:
        return "36"
    return "31"
