"""Trusted CLI startup for preparing one local Git workspace."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


_SETUP_MARKER = "# Added by coding-agent workspace setup"
_INITIAL_COMMIT_MESSAGE = "chore: initialize repository"
_GIT_TIMEOUT_SECONDS = 30
_MAX_ERROR_CHARS = 2_000

_BASE_IGNORE_PATTERNS = (
    ".env",
    ".env.*",
    "!.env.example",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.pem",
    ".coding-agent/",
    ".DS_Store",
    "Thumbs.db",
    ".idea/",
    ".vscode/",
)
_PYTHON_IGNORE_PATTERNS = (
    "__pycache__/",
    "*.py[cod]",
    ".venv/",
    "venv/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".coverage",
    "htmlcov/",
    "*.egg-info/",
    "build/",
    "dist/",
)
_NODE_IGNORE_PATTERNS = (
    "node_modules/",
    ".npm/",
    ".yarn/",
    ".pnpm-store/",
    "coverage/",
    "dist/",
)
_CPP_IGNORE_PATTERNS = (
    "cmake-build-*/",
    "CMakeFiles/",
    "CMakeCache.txt",
    "build/",
    "*.obj",
    "*.o",
)

_GIT_ENVIRONMENT_KEYS = {
    "COMSPEC",
    "EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_NAME",
    "GIT_COMMITTER_DATE",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_SYSTEM",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
    "XDG_CONFIG_HOME",
}


class WorkspaceSetupError(ValueError):
    """A workspace could not be safely prepared for the agent."""


@dataclass(frozen=True, slots=True)
class WorkspaceSetupResult:
    workspace: Path
    initialized: bool
    gitignore_updated: bool
    initial_commit: str | None
    messages: tuple[str, ...] = ()


def prepare_workspace(path: Path) -> WorkspaceSetupResult:
    """Return a usable Git root, initializing a safe baseline when needed."""

    workspace = path.expanduser().resolve()
    if not workspace.is_dir():
        raise WorkspaceSetupError("workspace does not exist or is not a directory")

    _run_git(workspace, "--version")
    top_level = _probe_git_top_level(workspace)
    if top_level is not None:
        if top_level != workspace:
            raise WorkspaceSetupError(
                f"workspace is inside Git repository at {top_level}; use that repository root"
            )
        _run_git(workspace, "status", "--porcelain=v1")
        return WorkspaceSetupResult(workspace, False, False, None)

    if (workspace / ".git").exists():
        raise WorkspaceSetupError("workspace .git entry exists but is not usable")

    identity = _run_git(workspace, "var", "GIT_AUTHOR_IDENT", check=False)
    if identity.returncode != 0:
        raise WorkspaceSetupError(
            "Git author identity is not configured; set user.name and user.email before setup"
        )

    _run_git(workspace, "init", "-q")
    gitignore_updated = _merge_gitignore(workspace)
    _run_git(workspace, "add", "--all")
    _run_git(workspace, "commit", "-q", "-m", _INITIAL_COMMIT_MESSAGE)

    status = _run_git(workspace, "status", "--porcelain=v1").stdout.strip()
    if status:
        raise WorkspaceSetupError(
            "initial commit completed but the workspace is not clean: "
            + _bounded(status)
        )
    commit = _run_git(workspace, "rev-parse", "--short=12", "HEAD").stdout.strip()
    if not commit:
        raise WorkspaceSetupError("initial commit completed but HEAD could not be resolved")

    messages = ["Initialized Git repository."]
    if gitignore_updated:
        messages.append("Added protective and project-aware .gitignore rules.")
    messages.append(f"Created initial commit {commit}.")
    return WorkspaceSetupResult(
        workspace=workspace,
        initialized=True,
        gitignore_updated=gitignore_updated,
        initial_commit=commit,
        messages=tuple(messages),
    )


def _probe_git_top_level(workspace: Path) -> Path | None:
    result = _run_git(workspace, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        raise WorkspaceSetupError("Git returned an empty repository root")
    return Path(raw).resolve()


def _merge_gitignore(workspace: Path) -> bool:
    path = workspace / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeError) as exc:
        raise WorkspaceSetupError(f"could not read .gitignore: {exc}") from exc

    prefix = existing
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += "\n"
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    managed_patterns = _required_ignore_patterns(workspace)
    updated = prefix + _SETUP_MARKER + "\n" + "\n".join(managed_patterns) + "\n"
    _atomic_write_text(path, updated)
    return True


def _required_ignore_patterns(workspace: Path) -> tuple[str, ...]:
    patterns = list(_BASE_IGNORE_PATTERNS)
    if _is_python_project(workspace):
        patterns.extend(_PYTHON_IGNORE_PATTERNS)
    if (workspace / "package.json").is_file():
        patterns.extend(_NODE_IGNORE_PATTERNS)
    if _is_cpp_project(workspace):
        patterns.extend(_CPP_IGNORE_PATTERNS)
    return tuple(dict.fromkeys(patterns))


def _is_python_project(workspace: Path) -> bool:
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile")
    return any((workspace / marker).is_file() for marker in markers) or any(
        workspace.glob("requirements*.txt")
    )


def _is_cpp_project(workspace: Path) -> bool:
    if (workspace / "CMakeLists.txt").is_file():
        return True
    return any(workspace.glob("*.sln")) or any(workspace.glob("*.vcxproj"))


def _atomic_write_text(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=".gitignore.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise WorkspaceSetupError(f"could not write .gitignore: {exc}") from exc


def _run_git(
    workspace: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            shell=False,
            check=False,
            env=_git_environment(),
        )
    except FileNotFoundError as exc:
        raise WorkspaceSetupError("Git executable was not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceSetupError(
            f"Git command timed out: git {' '.join(args)}"
        ) from exc

    if check and result.returncode != 0:
        detail = _bounded(result.stderr.strip() or result.stdout.strip() or "unknown error")
        raise WorkspaceSetupError(
            f"Git command failed ({result.returncode}): git {' '.join(args)}: {detail}"
        )
    return result


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _GIT_ENVIRONMENT_KEYS
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _bounded(value: str) -> str:
    if len(value) <= _MAX_ERROR_CHARS:
        return value
    return value[:_MAX_ERROR_CHARS] + "...[truncated]"
