"""Shared, dependency-free terminal styling and compact presentation helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TextIO


_COLORS = {
    "accent": "1;36",
    "muted": "2",
    "strong": "1",
    "success": "1;32",
    "warning": "1;33",
    "danger": "1;31",
    "info": "1;36",
    "selected": "30;46",
}

_ACTION_LABELS = {
    "list_directory": "List",
    "read_file": "Read",
    "search_text": "Search",
    "create_file": "Create",
    "write_file": "Write",
    "edit_file": "Edit",
    "delete_file": "Delete",
    "run_command": "Run",
    "git_status": "Git status",
    "git_diff": "Git diff",
    "browser_check": "Browser",
    "finish": "Finish",
    "respond": "Answer",
    "report_blocked": "Report blocker",
}


@dataclass(frozen=True, slots=True)
class TerminalTheme:
    """Own the visual language while callers retain terminal behavior."""

    enabled: bool

    @classmethod
    def for_stream(
        cls,
        output: TextIO,
        *,
        enabled: bool | None = None,
    ) -> "TerminalTheme":
        if enabled is None:
            is_tty = bool(getattr(output, "isatty", lambda: False)())
            enabled = is_tty and "NO_COLOR" not in os.environ
        return cls(bool(enabled))

    def paint(self, value: str, role: str) -> str:
        if not self.enabled or not value:
            return value
        return f"\x1b[{_COLORS[role]}m{value}\x1b[0m"

    def prompt(self) -> str:
        return self.paint("❯", "accent") + " "

    def banner(
        self,
        title: str,
        rows: tuple[tuple[str, str], ...],
        footer: str,
    ) -> str:
        width = max((len(label) for label, _ in rows), default=0)
        lines = [
            f"{self.paint('╭─', 'accent')} {self.paint(title, 'accent')}"
        ]
        for label, value in rows:
            lines.append(
                f"{self.paint('│', 'accent')}  "
                f"{self.paint(f'{label:<{width}}', 'muted')}  {value}"
            )
        lines.append(
            f"{self.paint('╰─', 'accent')} {self.paint(footer, 'muted')}"
        )
        return "\n".join(lines) + "\n"

    def menu_header(self, title: str, enter_action: str) -> str:
        title_text = self.paint(title, "strong")
        hint = self.paint(
            f"↑/↓ move · Enter {enter_action} · type filter · Esc cancel",
            "muted",
        )
        return f"{title_text}  {hint}"

    def selected_row(self, value: str) -> str:
        return self.paint(f"› {value}", "selected")

    def action_line(
        self,
        action: str,
        detail: str,
        rationale: str,
        repeated: int | None,
    ) -> str:
        details = [detail] if detail else []
        if repeated is not None and repeated > 1:
            details.append(f"repeated {repeated}x")
        suffix = f"  {' · '.join(details)}" if details else ""
        line = (
            f"{self.paint('●', 'accent')} "
            f"{self.paint(action_label(action), 'strong')}{suffix}"
        )
        if rationale:
            line += self.paint(f" — {rationale}", "muted")
        return line

    def working_line(self) -> str:
        return (
            f"{self.paint('◇', 'accent')} "
            f"{self.paint('Working…', 'muted')}"
        )

    def tool_result_line(
        self,
        tool: str,
        detail: str,
        status: str,
        timing: str,
    ) -> str:
        normalized = status.upper()
        symbol, tone = {
            "COMPLETED": ("✓", "success"),
            "FAILED": ("×", "danger"),
            "DENIED": ("!", "warning"),
            "CANCELLED": ("○", "warning"),
        }.get(normalized, ("◇", "info"))
        suffix = f"  {detail}" if detail else ""
        if normalized != "COMPLETED":
            suffix += f" · {normalized}"
        suffix += f" · {timing}"
        return (
            f"  {self.paint('└', 'muted')} {self.paint(symbol, tone)} "
            f"{self.paint(action_label(tool), 'strong')}{suffix}"
        )

    def cached_tool_line(
        self,
        tool: str,
        detail: str,
        state: str,
        reason: str,
    ) -> str:
        symbol = "◇" if state == "CACHED" else "○"
        suffix = f"  {detail}" if detail else ""
        suffix += f" · {state.lower()}"
        if reason:
            suffix += f" — {reason}"
        return (
            f"  {self.paint('└', 'muted')} {self.paint(symbol, 'info')} "
            f"{self.paint(action_label(tool), 'strong')}{suffix}"
        )

    def notice(self, label: str, message: str, *, tone: str = "info") -> str:
        return (
            f"  {self.paint('↳', 'muted')} "
            f"{self.paint(label, tone)}  {message}"
        )

    def terminal_line(self, status: str, summary: str) -> str:
        normalized = status.upper()
        symbol, label, tone = {
            "SUCCEEDED": ("✓", "Completed", "success"),
            "ANSWERED": ("◇", "Answer", "info"),
            "BLOCKED": ("!", "Blocked", "warning"),
            "CANCELLED": ("○", "Cancelled", "warning"),
            "FAILED": ("×", "Failed", "danger"),
        }.get(normalized, ("◇", normalized.title(), "info"))
        return (
            f"{self.paint(symbol, tone)} {self.paint(label, tone)}"
            + (f"  {summary}" if summary else "")
        )

    def approval_card(
        self,
        *,
        risk: str,
        action: str,
        request: str,
        description: str,
        digest: str,
    ) -> str:
        return self.banner(
            f"Approval required · {risk}",
            (
                ("Action", action),
                ("Request", request),
                ("Risk", description),
                ("Digest", digest),
            ),
            "y approve · d details · Enter deny",
        )


def action_label(action: str) -> str:
    """Return a short human label while keeping unknown actions intelligible."""

    known = _ACTION_LABELS.get(action)
    if known is not None:
        return known
    normalized = " ".join(part for part in action.replace("-", "_").split("_") if part)
    return normalized.capitalize() or "Unknown"
