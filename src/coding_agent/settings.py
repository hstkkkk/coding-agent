"""Validated per-user configuration for the coding-agent CLI."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


_MAX_SETTINGS_BYTES = 64 * 1024
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KNOWN_FIELDS = frozenset(
    {
        "api_key",
        "api_key_env",
        "model",
        "base_url",
        "thinking",
        "max_turns",
        "max_seconds",
        "command_timeout",
        "approval_mode",
        "allow_programs",
        "runs_dir",
    }
)


class SettingsError(ValueError):
    """The fixed per-user settings file could not be safely loaded."""


@dataclass(frozen=True, slots=True)
class UserSettings:
    """Validated optional overrides from one per-user settings file."""

    api_key: str | None = field(default=None, repr=False)
    api_key_env: str | None = None
    model: str | None = None
    base_url: str | None = None
    thinking: str | None = None
    max_turns: int | None = None
    max_seconds: int | None = None
    command_timeout: int | None = None
    approval_mode: str | None = None
    allow_programs: tuple[str, ...] = ()
    runs_dir: Path | None = None
    provided_fields: frozenset[str] = field(
        default_factory=frozenset,
        repr=False,
    )

    def provides(self, field_name: str) -> bool:
        """Return whether the JSON file explicitly supplied a field."""

        return field_name in self.provided_fields


def default_settings_path() -> Path:
    """Return the single supported per-user settings path."""

    try:
        return (Path.home() / ".coding-agent" / "settings.json").resolve()
    except RuntimeError as exc:
        raise SettingsError("could not determine the user home directory") from exc


def load_user_settings(path: Path | None = None) -> UserSettings:
    """Load and validate settings, returning empty overrides when absent."""

    settings_path = (path or default_settings_path()).expanduser().resolve()
    try:
        with settings_path.open("rb") as handle:
            encoded = handle.read(_MAX_SETTINGS_BYTES + 1)
    except FileNotFoundError:
        return UserSettings()
    except OSError as exc:
        raise SettingsError(
            f"could not read user settings file at {settings_path}"
        ) from exc

    if len(encoded) > _MAX_SETTINGS_BYTES:
        raise SettingsError("user settings file exceeds the 64 KiB size limit")
    try:
        text = encoded.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SettingsError("user settings file must be UTF-8 JSON") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(
            "user settings file is not valid JSON "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(raw, dict):
        raise SettingsError("user settings file must contain one JSON object")

    unknown = sorted(set(raw) - _KNOWN_FIELDS)
    if len(unknown) == 1:
        raise SettingsError(f"unknown settings field: {unknown[0]}")
    if unknown:
        raise SettingsError(f"unknown settings fields: {', '.join(unknown)}")

    return UserSettings(
        api_key=_optional_string(raw, "api_key"),
        api_key_env=_optional_environment_name(raw, "api_key_env"),
        model=_optional_string(raw, "model"),
        base_url=_optional_base_url(raw),
        thinking=_optional_choice(
            raw,
            "thinking",
            {"enabled", "disabled"},
            allow_null=True,
        ),
        max_turns=_optional_integer(raw, "max_turns", 1, 200),
        max_seconds=_optional_integer(raw, "max_seconds", 1, 7_200),
        command_timeout=_optional_integer(raw, "command_timeout", 1, 600),
        approval_mode=_optional_choice(raw, "approval_mode", {"prompt", "deny"}),
        allow_programs=_optional_programs(raw),
        runs_dir=_optional_runs_dir(raw, settings_path.parent),
        provided_fields=frozenset(raw),
    )


def _optional_string(raw: dict[str, Any], field_name: str) -> str | None:
    if field_name not in raw:
        return None
    value = raw[field_name]
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"settings field '{field_name}' must be a non-empty string")
    return value.strip()


def _optional_environment_name(raw: dict[str, Any], field_name: str) -> str | None:
    value = _optional_string(raw, field_name)
    if value is not None and _ENVIRONMENT_NAME.fullmatch(value) is None:
        raise SettingsError(
            f"settings field '{field_name}' must be an environment variable name"
        )
    return value


def _optional_base_url(raw: dict[str, Any]) -> str | None:
    value = _optional_string(raw, "base_url")
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SettingsError(
            "settings field 'base_url' must be an absolute HTTP(S) URL"
        )
    return value


def _optional_choice(
    raw: dict[str, Any],
    field_name: str,
    choices: set[str],
    *,
    allow_null: bool = False,
) -> str | None:
    if field_name not in raw:
        return None
    value = raw[field_name]
    if allow_null and value is None:
        return None
    if not isinstance(value, str) or value not in choices:
        allowed = ", ".join(sorted(choices))
        suffix = ", or null" if allow_null else ""
        raise SettingsError(
            f"settings field '{field_name}' must be {allowed}{suffix}"
        )
    return value


def _optional_integer(
    raw: dict[str, Any],
    field_name: str,
    minimum: int,
    maximum: int,
) -> int | None:
    if field_name not in raw:
        return None
    value = raw[field_name]
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise SettingsError(
            f"settings field '{field_name}' must be an integer between "
            f"{minimum} and {maximum}"
        )
    return value


def _optional_programs(raw: dict[str, Any]) -> tuple[str, ...]:
    if "allow_programs" not in raw:
        return ()
    values = raw["allow_programs"]
    if not isinstance(values, list):
        raise SettingsError("settings field 'allow_programs' must be an array")
    normalized: list[str] = []
    observed: set[str] = set()
    for value in values:
        if (
            not isinstance(value, str)
            or not value.strip()
            or any(separator in value for separator in ("/", "\\"))
        ):
            raise SettingsError(
                "settings field 'allow_programs' must contain executable names"
            )
        program = value.strip()
        identity = program.casefold()
        if identity not in observed:
            observed.add(identity)
            normalized.append(program)
    return tuple(normalized)


def _optional_runs_dir(raw: dict[str, Any], base_directory: Path) -> Path | None:
    value = _optional_string(raw, "runs_dir")
    if value is None:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_directory / candidate
    try:
        return candidate.resolve()
    except OSError as exc:
        raise SettingsError("settings field 'runs_dir' is not a valid path") from exc
