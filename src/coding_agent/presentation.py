"""Bounded, content-safe descriptions of model-visible tool activity."""

from __future__ import annotations

import unicodedata
from typing import Any, Mapping


_DETAIL_LIMIT = 120
_INLINE_FLAGS = {"-c", "-e", "--eval", "--execute"}


def describe_tool(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """Describe a proposed tool call without rendering file or script content."""

    if tool_name == "list_directory":
        parts = [_path(arguments)]
        if arguments.get("recursive") is True:
            parts.append("recursive")
        return _join(parts)
    if tool_name == "read_file":
        parts = [_path(arguments)]
        start = arguments.get("start_line")
        end = arguments.get("end_line")
        if isinstance(start, int) or isinstance(end, int):
            parts.append(f"lines={start or 1}-{end or 'end'}")
        return _join(parts)
    if tool_name == "search_text":
        query = arguments.get("query")
        length = len(query) if isinstance(query, str) else 0
        return _join([_path(arguments), f"query={length} chars"])
    if tool_name == "edit_file":
        return _path(arguments)
    if tool_name in {"create_file", "write_file"}:
        content = arguments.get("content")
        length = len(content) if isinstance(content, str) else 0
        return _join([_path(arguments), f"{length} chars"])
    if tool_name == "delete_file":
        return _path(arguments)
    if tool_name == "run_command":
        return _describe_command(arguments)
    if tool_name == "git_status":
        return "workspace"
    if tool_name == "git_diff":
        return "staged diff" if arguments.get("staged") is True else "working-tree diff"
    if tool_name == "read_output":
        return _output_reference(arguments)
    if tool_name == "search_output":
        pattern = arguments.get("pattern")
        length = len(pattern) if isinstance(pattern, str) else 0
        return _join([_output_reference(arguments), f"pattern={length} chars"])
    return ""


def describe_tool_result(
    tool_name: str,
    arguments: Mapping[str, Any],
    data: Mapping[str, Any],
) -> str:
    """Describe a completed tool call using its most useful observable outcome."""

    if tool_name == "list_directory":
        entries = data.get("entries")
        count = len(entries) if isinstance(entries, list) else 0
        return _join([_path(arguments), _count(count, "entry", "entries")])
    if tool_name == "read_file":
        content = data.get("content")
        parts = [_path(arguments)]
        start = data.get("start_line")
        end = data.get("end_line")
        if isinstance(start, int) and isinstance(end, int):
            parts.append(f"lines={start}-{end}")
        if isinstance(content, str):
            parts.append(f"{len(content)} chars")
        return _join(parts)
    if tool_name == "search_text":
        matches = data.get("matches")
        count = len(matches) if isinstance(matches, list) else 0
        return _join([_path(arguments), _count(count, "match")])
    if tool_name in {"edit_file", "write_file", "create_file", "delete_file"}:
        parts = [_path(arguments)]
        if data.get("workspace_changed") is True or tool_name == "delete_file":
            parts.append("changed")
        return _join(parts)
    if tool_name == "run_command":
        program = _display(arguments.get("program", "?"))
        exit_code = data.get("exit_code")
        parts = [f"program={program}"]
        if isinstance(exit_code, int):
            parts.append(f"exit={exit_code}")
        if data.get("workspace_changed") is True:
            parts.append("workspace changed")
        return _join(parts)
    if tool_name == "git_status":
        changed = data.get("changed_files")
        count = len(changed) if isinstance(changed, list) else 0
        return _count(count, "changed file")
    if tool_name == "git_diff":
        preview = data.get("diff_preview")
        length = len(preview) if isinstance(preview, str) else 0
        return _join([describe_tool(tool_name, arguments), f"{length} chars"])
    if tool_name in {"read_output", "search_output"}:
        return _output_reference(arguments)
    return describe_tool(tool_name, arguments)


def _describe_command(arguments: Mapping[str, Any]) -> str:
    program = _display(arguments.get("program", "?"))
    cwd = _display(arguments.get("cwd", "."))
    purpose = _display(arguments.get("purpose", "inspect"))
    values = arguments.get("args")
    args = values if isinstance(values, list) else []
    parts = [
        f"program={program}",
        f"cwd={cwd}",
        f"purpose={purpose}",
        f"{len(args)} args",
    ]
    if (
        len(args) >= 2
        and isinstance(args[0], str)
        and args[0] in _INLINE_FLAGS
        and isinstance(args[1], str)
    ):
        parts.append(f"inline code={len(args[1])} chars")
    return _join(parts)


def _path(arguments: Mapping[str, Any]) -> str:
    return f"path={_display(arguments.get('path', '.'))}"


def _output_reference(arguments: Mapping[str, Any]) -> str:
    value = arguments.get("output_id")
    if not isinstance(value, str) or not value:
        return "output=?"
    return f"output={_display(value[:12])}"


def _display(value: Any) -> str:
    raw = str(value)
    rendered: list[str] = []
    for character in raw[:_DETAIL_LIMIT]:
        if unicodedata.category(character).startswith("C"):
            rendered.append(f"\\u{ord(character):04x}")
        else:
            rendered.append(character)
    if len(raw) > _DETAIL_LIMIT:
        rendered.append("…")
    return "".join(rendered)


def _count(value: int, noun: str, plural: str | None = None) -> str:
    label = noun if value == 1 else (plural or noun + "s")
    return f"{value} {label}"


def _join(parts: list[str]) -> str:
    return " · ".join(part for part in parts if part)
