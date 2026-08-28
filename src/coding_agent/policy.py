"""Workspace policy and approval adapters."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .domain import ApprovalDecision, ApprovalPort, ApprovalRequest, RiskLevel


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
    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        print(f"Approval required [{request.risk.value}]: {request.summary}")
        print(f"Operation digest: {request.operation_digest[:12]}")
        try:
            answer = input("Approve this exact operation? [y/N] ").strip().lower()
        except EOFError:
            return ApprovalDecision(False, "no interactive input was available")
        if answer in {"y", "yes"}:
            return ApprovalDecision(True, "approved interactively")
        return ApprovalDecision(False, "declined interactively")


def needs_approval(risk: RiskLevel) -> bool:
    return risk in {RiskLevel.DELETE, RiskLevel.EXECUTION}
