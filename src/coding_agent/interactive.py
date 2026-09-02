"""Line-oriented terminal UI for repeated bounded agent runs."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Callable, TextIO

from .conversation import ConversationError, ConversationSession, SessionInfo
from .domain import RunResult, RunStatus
from .terminal import CommandChoice, TerminalPrompt


_COMMANDS = (
    CommandChoice("/help", "Show this help."),
    CommandChoice("/workspace", "Show the active repository root."),
    CommandChoice("/session", "Show the short reference and full session ID."),
    CommandChoice("/resume", "Choose another session in this workspace."),
    CommandChoice("/history", "Show persisted turns from this session."),
    CommandChoice("/clear", "Clear an ANSI-capable terminal."),
    CommandChoice("/exit", "End the session."),
)


class InteractiveSession:
    """Own terminal presentation around one persistent conversation."""

    def __init__(
        self,
        *,
        conversation: ConversationSession,
        model_label: str,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        styled: bool | None = None,
    ) -> None:
        self.conversation = conversation
        self.workspace = conversation.workspace
        self.model_label = model_label
        self.input = input_stream or sys.stdin
        self.output = output_stream or sys.stdout
        self.styled = self._detect_styling() if styled is None else styled
        self.prompt = TerminalPrompt(
            commands=_COMMANDS,
            input_stream=self.input,
            output_stream=self.output,
        )

    def run(self, run_task: Callable[[str], RunResult]) -> int:
        self._render_banner()
        while True:
            try:
                raw = self._readline()
            except KeyboardInterrupt:
                self._write("\nInput cancelled. Type /exit to end the session.\n")
                continue

            if raw == "":
                self._render_exit()
                return 0
            request = raw.strip()
            if not request:
                continue
            if request.startswith("/"):
                if self._handle_command(request):
                    return 0
                continue

            try:
                prepared = self.conversation.prepare(request)
            except ConversationError as exc:
                self._write(f"[SESSION] Could not prepare persistent context: {exc}\n")
                return 5
            if prepared.compacted_now:
                self._write(
                    f"[SESSION] Compacted {prepared.compacted_now} older turn(s) "
                    "into persistent memory.\n"
                )
            try:
                result = run_task(prepared.objective)
            except KeyboardInterrupt:
                self._write("\n[SESSION] Task interrupted; no success was recorded.\n")
                continue

            try:
                self.conversation.record(request, result)
            except ConversationError as exc:
                self._write(f"[SESSION] Could not persist completed turn: {exc}\n")
                return 5
            self._render_result(result)

    def _readline(self) -> str:
        return self.prompt.readline(self._style("coding-agent> ", "36"))

    def _handle_command(self, raw: str) -> bool:
        command_text, _, argument = raw.partition(" ")
        command = command_text.lower()
        if command in {"/exit", "/quit"}:
            self._render_exit()
            return True
        if command == "/help":
            width = max(len(choice.command) for choice in _COMMANDS)
            self._write("Commands:\n")
            for choice in _COMMANDS:
                self._write(
                    f"  {choice.command:<{width}}  {choice.description}\n"
                )
            return False
        if command == "/workspace":
            self._write(f"Workspace: {self.workspace}\n")
            return False
        if command == "/session":
            self._write(
                f"Session: {self.conversation.reference} "
                f"(full: {self.conversation.session_id})\n"
            )
            return False
        if command == "/resume":
            self._resume_session(argument.strip() or None)
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
            + f"\nSession: {self.conversation.reference} "
            + ("(resumed)" if self.conversation.resumed else "(new)")
            + "\nType / to browse commands, or /exit to quit.\n\n"
        )

    def _render_result(self, result: RunResult) -> None:
        status = self._style(result.status.value, _status_color(result.status))
        self._write(f"[SESSION] {status} | Run ID: {result.run_id}\n")
        if result.changed_files:
            self._write("[SESSION] Changed: " + ", ".join(result.changed_files) + "\n")

    def _render_history(self) -> None:
        try:
            history = self.conversation.history()
        except ConversationError as exc:
            self._write(f"Could not read session history: {exc}\n")
            return
        if history.total_turns == 0:
            self._write("No tasks have run in this session.\n")
            return
        if history.compacted_turns:
            self._write(
                f"{history.compacted_turns} earlier turn(s) are in compacted memory.\n"
            )
        for entry in history.entries:
            request = " ".join(entry.request.split())
            self._write(
                f"{entry.index}. {entry.status.value} {entry.run_id} | {request}\n"
            )

    def _render_exit(self) -> None:
        reference = self.conversation.reference
        try:
            discarded = self.conversation.discard_if_empty()
        except ConversationError as exc:
            self._write(f"\nCould not clean up empty session: {exc}\n")
            discarded = False
        if discarded:
            self._write("\nNo completed tasks; empty session was not saved.\n")
            return
        self._write(
            "\nSession ended. Resume with: coding-agent resume "
            f"{reference}\n"
        )

    def _resume_session(self, reference: str | None) -> None:
        try:
            sessions = self.conversation.resumable_sessions(limit=20)
        except ConversationError as exc:
            self._write(f"Could not list saved sessions: {exc}\n")
            return
        alternatives = tuple(
            session
            for session in sessions
            if session.session_id != self.conversation.session_id
        )
        selected = reference
        if selected is None:
            if not alternatives:
                self._write("No other sessions were found for this workspace.\n")
                return
            selected = self.prompt.select(
                "Resume session",
                tuple(
                    CommandChoice(session.reference, _session_description(session))
                    for session in alternatives
                ),
            )
            if selected is None:
                self._write("Resume cancelled.\n")
                return

        try:
            resumed = self.conversation.switch(selected)
        except ConversationError as exc:
            self._write(f"Could not resume session: {exc}\n")
            return
        previous = self.conversation
        if resumed.session_id != previous.session_id:
            try:
                previous.discard_if_empty()
            except ConversationError as exc:
                self._write(f"Could not clean up previous empty session: {exc}\n")
        self.conversation = resumed
        self.workspace = resumed.workspace
        info = next(
            (item for item in sessions if item.session_id == resumed.session_id),
            None,
        )
        suffix = f" · {_session_description(info)}" if info is not None else ""
        self._write(f"Resumed session {resumed.reference}{suffix}\n")

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
    if status is RunStatus.ANSWERED:
        return "36"
    if status is RunStatus.BLOCKED:
        return "33"
    if status is RunStatus.CANCELLED:
        return "36"
    return "31"


def _session_description(session: SessionInfo) -> str:
    timestamp = session.last_turn_at or session.created_at
    time_label = _local_time(timestamp)
    request = _one_line(session.last_request) if session.last_request else "no completed request"
    turn_label = "turn" if session.turn_count == 1 else "turns"
    return f"{time_label} · {session.turn_count} {turn_label} · last: {request}"


def _local_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return "unknown time"


def _one_line(value: str, *, limit: int = 80) -> str:
    printable = "".join(character if character.isprintable() else " " for character in value)
    normalized = " ".join(printable.split()) or "[empty]"
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."
