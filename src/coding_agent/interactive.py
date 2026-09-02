"""Line-oriented terminal UI for repeated bounded agent runs."""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Callable, TextIO

from .conversation import ConversationError, ConversationSession, SessionInfo
from .domain import RunResult
from .terminal import CommandChoice, TerminalPrompt
from .terminal_ui import TerminalTheme


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
        thinking_label: str = "provider default",
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        styled: bool | None = None,
    ) -> None:
        self.conversation = conversation
        self.workspace = conversation.workspace
        self.model_label = model_label
        self.thinking_label = thinking_label
        self.input = input_stream or sys.stdin
        self.output = output_stream or sys.stdout
        self.theme = TerminalTheme.for_stream(self.output, enabled=styled)
        self.styled = self.theme.enabled
        self.prompt = TerminalPrompt(
            commands=_COMMANDS,
            input_stream=self.input,
            output_stream=self.output,
            styled=self.styled,
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
                self._write_session_error(
                    f"Could not prepare persistent context: {exc}"
                )
                return 5
            if prepared.compacted_now:
                message = (
                    f"Compacted {prepared.compacted_now} older turn(s) "
                    "into persistent memory."
                )
                if self.styled:
                    self._write(self.theme.notice("Context", message) + "\n")
                else:
                    self._write(f"[SESSION] {message}\n")
            try:
                result = run_task(prepared.objective)
            except KeyboardInterrupt:
                if self.styled:
                    self._write(
                        "\n"
                        + self.theme.notice(
                            "Interrupted",
                            "Task stopped; no success was recorded.",
                            tone="warning",
                        )
                        + "\n"
                    )
                else:
                    self._write(
                        "\n[SESSION] Task interrupted; no success was recorded.\n"
                    )
                continue

            try:
                self.conversation.record(request, result)
            except ConversationError as exc:
                self._write_session_error(f"Could not persist completed turn: {exc}")
                return 5
            self._render_result(result)

    def _readline(self) -> str:
        prompt = self.theme.prompt() if self.styled else "coding-agent> "
        return self.prompt.readline(prompt)

    def _handle_command(self, raw: str) -> bool:
        command_text, _, argument = raw.partition(" ")
        command = command_text.lower()
        if command in {"/exit", "/quit"}:
            self._render_exit()
            return True
        if command == "/help":
            width = max(len(choice.command) for choice in _COMMANDS)
            heading = (
                self.theme.paint("Commands", "strong")
                if self.styled
                else "Commands:"
            )
            self._write(heading + "\n")
            for choice in _COMMANDS:
                command_label = f"{choice.command:<{width}}"
                if self.styled:
                    command_label = self.theme.paint(command_label, "accent")
                self._write(f"  {command_label}  {choice.description}\n")
            return False
        if command == "/workspace":
            if self.styled:
                self._write(self.theme.notice("Workspace", str(self.workspace)) + "\n")
            else:
                self._write(f"Workspace: {self.workspace}\n")
            return False
        if command == "/session":
            message = (
                f"{self.conversation.reference} "
                f"(full: {self.conversation.session_id})"
            )
            if self.styled:
                self._write(self.theme.notice("Session", message) + "\n")
            else:
                self._write(f"Session: {message}\n")
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
        if self.styled:
            state = "resumed" if self.conversation.resumed else "new"
            self._write(
                self.theme.banner(
                    "Bounded Coding Agent",
                    (
                        ("Workspace", str(self.workspace)),
                        (
                            "Model",
                            f"{self.model_label} · thinking {self.thinking_label}",
                        ),
                        ("Session", f"{self.conversation.reference} · {state}"),
                    ),
                    "/ commands · /exit quit",
                )
                + "\n"
            )
            return
        self._write(
            "Bounded Coding Agent"
            + f"\nWorkspace: {self.workspace}"
            + f"\nModel: {self.model_label}"
            + f"\nThinking: {self.thinking_label}"
            + f"\nSession: {self.conversation.reference} "
            + ("(resumed)" if self.conversation.resumed else "(new)")
            + "\nType / to browse commands, or /exit to quit.\n\n"
        )

    def _render_result(self, result: RunResult) -> None:
        if self.styled:
            tone = {
                "SUCCEEDED": "success",
                "ANSWERED": "info",
                "BLOCKED": "warning",
                "CANCELLED": "warning",
                "FAILED": "danger",
            }.get(result.status.value, "info")
            self._write(
                self.theme.notice(
                    "Run",
                    f"{result.run_id} · {result.status.value.lower()}",
                    tone=tone,
                )
                + "\n"
            )
            if result.changed_files:
                changed = ", ".join(result.changed_files)
                self._write(self.theme.notice("Changed", changed) + "\n")
            self._write("\n")
            return
        self._write(f"[SESSION] {result.status.value} | Run ID: {result.run_id}\n")
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
        if self.styled:
            self._write(self.theme.paint("History", "strong") + "\n")
        if history.compacted_turns:
            self._write(
                f"{history.compacted_turns} earlier turn(s) are in compacted memory.\n"
            )
        for entry in history.entries:
            request = " ".join(entry.request.split())
            if self.styled:
                status = self.theme.terminal_line(entry.status.value, "")
                self._write(
                    f"  {entry.index:>2}  {status}  "
                    f"{self.theme.paint(entry.run_id[:8], 'muted')}  {request}\n"
                )
            else:
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
            message = "No completed tasks; empty session was not saved."
            if self.styled:
                self._write("\n" + self.theme.notice("Session", message) + "\n")
            else:
                self._write("\n" + message + "\n")
            return
        message = f"Resume with: coding-agent resume {reference}"
        if self.styled:
            self._write("\n" + self.theme.notice("Session saved", message) + "\n")
        else:
            self._write("\nSession ended. " + message + "\n")

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
        message = f"{resumed.reference}{suffix}"
        if self.styled:
            self._write(self.theme.notice("Resumed", message) + "\n")
        else:
            self._write(f"Resumed session {message}\n")

    def _write_session_error(self, message: str) -> None:
        if self.styled:
            self._write(
                self.theme.notice("Session error", message, tone="danger") + "\n"
            )
        else:
            self._write(f"[SESSION] {message}\n")

    def _write(self, value: str, *, flush: bool = False) -> None:
        self.output.write(value)
        if flush:
            self.output.flush()


def _session_description(session: SessionInfo) -> str:
    timestamp = session.last_turn_at or session.created_at
    time_label = _local_time(timestamp)
    request = (
        _one_line(session.last_request)
        if session.last_request
        else "no completed request"
    )
    turn_label = "turn" if session.turn_count == 1 else "turns"
    return f"{time_label} · {session.turn_count} {turn_label} · last: {request}"


def _local_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return "unknown time"


def _one_line(value: str, *, limit: int = 80) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in value
    )
    normalized = " ".join(printable.split()) or "[empty]"
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."
