"""Workspace policy and approval adapters."""

from __future__ import annotations

import hashlib
import json
import sys
import unicodedata
from pathlib import Path
from typing import TextIO

from .domain import ApprovalDecision, ApprovalPort, ApprovalRequest, RiskLevel
from .terminal_ui import TerminalTheme


class PolicyViolation(ValueError):
    pass


class PathPolicy:
    _SENSITIVE_NAMES = {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
    _SENSITIVE_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise PolicyViolation("workspace must be a directory")

    def resolve(self, raw_path: str, *, write: bool = False) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise PolicyViolation("path must be a non-empty workspace-relative string")
        relative = Path(raw_path)
        if relative.is_absolute() or relative.drive or raw_path.startswith(("\\\\", "//")):
            raise PolicyViolation("absolute paths are not allowed")

        candidate = (self.workspace / relative).resolve(strict=False)
        try:
            normalized = candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise PolicyViolation("path escapes the workspace") from exc

        lowered_parts = {part.lower() for part in normalized.parts}
        if ".git" in lowered_parts:
            raise PolicyViolation("direct file access to .git is not allowed")
        if ".coding-agent" in lowered_parts:
            raise PolicyViolation(
                "direct file access to .coding-agent is not allowed"
            )
        if self._is_sensitive(normalized):
            raise PolicyViolation("access to a likely credential file is not allowed")
        return candidate

    @classmethod
    def _is_sensitive(cls, relative: Path) -> bool:
        name = relative.name.lower()
        if name == ".env.example":
            return False
        if name in cls._SENSITIVE_NAMES or name.startswith(".env."):
            return True
        return relative.suffix.lower() in cls._SENSITIVE_SUFFIXES


def operation_digest(tool_name: str, arguments: dict[str, object]) -> str:
    encoded = json.dumps(
        {"tool": tool_name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DenyApprovalAdapter(ApprovalPort):
    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(False, "non-interactive policy denied the operation")


class AllowApprovalAdapter(ApprovalPort):
    """Explicitly pre-authorized adapter, primarily for controlled evaluation."""

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(True, "operation was explicitly pre-authorized")


class FixedApprovalAdapter(ApprovalPort):
    def __init__(self, approved: bool, reason: str = "fixed test decision") -> None:
        self.approved = approved
        self.reason = reason
        self.requests: list[ApprovalRequest] = []

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(self.approved, self.reason)


class PromptApprovalAdapter(ApprovalPort):
    def __init__(
        self,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        detail_width: int = 100,
        styled: bool | None = None,
    ) -> None:
        self.input = input_stream or sys.stdin
        self.output = output_stream or sys.stdout
        self.detail_width = max(40, detail_width)
        self.theme = TerminalTheme.for_stream(self.output, enabled=styled)

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        if self.theme.enabled:
            self._write(
                "\n"
                + self.theme.approval_card(
                    risk=request.risk.value,
                    action=_approval_action(request.tool_name),
                    request=request.summary or request.tool_name,
                    description=_risk_description(request.risk),
                    digest=request.operation_digest[:12],
                )
            )
        else:
            self._write(
                f"\nApproval required [{request.risk.value}]\n"
                f"  Action: {_approval_action(request.tool_name)}\n"
                f"  Request: {request.summary or request.tool_name}\n"
                f"  Risk: {_risk_description(request.risk)}\n"
                f"  Operation digest: {request.operation_digest[:12]}\n"
            )
        while True:
            prompt = (
                self.theme.prompt()
                + self.theme.paint(
                    "Approve this digest? [y]es · [d]etails · [N]o: ",
                    "muted",
                )
                if self.theme.enabled
                else "Approve this exact digest? [y]es / [d]etails / [N]o: "
            )
            self._write(prompt, flush=True)
            answer = self.input.readline()
            if answer == "":
                return ApprovalDecision(False, "no interactive input was available")
            choice = answer.strip().lower()
            if choice in {"y", "yes"}:
                return ApprovalDecision(True, "approved interactively")
            if choice in {"", "n", "no"}:
                return ApprovalDecision(False, "declined interactively")
            if choice in {"d", "detail", "details"}:
                self._render_details(request)
                continue
            message = "Enter y to approve, d for details, or n to deny."
            if self.theme.enabled:
                self._write(
                    self.theme.notice("Invalid choice", message, tone="warning")
                    + "\n"
                )
            else:
                self._write(message + "\n")

    def _render_details(self, request: ApprovalRequest) -> None:
        encoded = json.dumps(
            {"tool": request.tool_name, "arguments": request.arguments},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        encoded = "".join(
            f"\\u{ord(character):04x}"
            if unicodedata.category(character) == "Cf"
            else character
            for character in encoded
        )
        if self.theme.enabled:
            self._write(
                "\n"
                + self.theme.paint("Full arguments", "strong")
                + self.theme.paint(
                    " · redacted JSON · visual wrapping only", "muted"
                )
                + "\n"
            )
        else:
            self._write(
                "\nFull arguments (redacted JSON; visual wrapping only):\n"
            )
        for line in encoded.splitlines() or [""]:
            if not line:
                self._write("\n")
                continue
            for offset in range(0, len(line), self.detail_width):
                self._write("  " + line[offset : offset + self.detail_width] + "\n")
        if self.theme.enabled:
            self._write(
                self.theme.paint("Exact digest", "muted")
                + f"  {request.operation_digest}\n\n"
            )
        else:
            self._write(f"Exact operation digest: {request.operation_digest}\n\n")

    def _write(self, value: str, *, flush: bool = False) -> None:
        self.output.write(value)
        if flush:
            self.output.flush()


class ScopedApprovalAdapter(ApprovalPort):
    """Pre-approve execution only for explicitly named executables."""

    def __init__(self, fallback: ApprovalPort, allowed_programs: set[str]) -> None:
        self._fallback = fallback
        self._allowed_programs = {item.lower() for item in allowed_programs}

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        program = request.arguments.get("program")
        if (
            request.tool_name == "run_command"
            and isinstance(program, str)
            and program.lower() in self._allowed_programs
        ):
            return ApprovalDecision(
                True,
                f"program {program} was explicitly pre-authorized at startup",
            )
        return self._fallback.request(request)


def needs_approval(risk: RiskLevel) -> bool:
    return risk in {RiskLevel.OVERWRITE, RiskLevel.DELETE, RiskLevel.EXECUTION}


def _approval_action(tool_name: str) -> str:
    return {
        "run_command": "Run a local program",
        "browser_check": "Render local HTML in a headless browser",
        "write_file": "Replace a workspace file",
        "delete_file": "Delete a workspace file",
    }.get(tool_name, tool_name)


def _risk_description(risk: RiskLevel) -> str:
    if risk is RiskLevel.EXECUTION:
        return "Executes code with your operating-system account permissions."
    if risk is RiskLevel.OVERWRITE:
        return "Replaces the complete contents of the named workspace file."
    if risk is RiskLevel.DELETE:
        return "Permanently removes the named workspace file."
    return "Changes the current workspace."
