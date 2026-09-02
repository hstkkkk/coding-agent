"""Small terminal-prompt interface with a slash-command selector."""

from __future__ import annotations

import os
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterator, TextIO


class TerminalKey(Enum):
    ENTER = "enter"
    BACKSPACE = "backspace"
    DELETE = "delete"
    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"
    HOME = "home"
    END = "end"
    TAB = "tab"
    ESCAPE = "escape"
    INTERRUPT = "interrupt"
    EOF = "eof"


@dataclass(frozen=True, slots=True)
class CommandChoice:
    command: str
    description: str


class TerminalPrompt:
    """Read one editable request and own command-menu keyboard behavior."""

    def __init__(
        self,
        *,
        commands: tuple[CommandChoice, ...],
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        interactive: bool | None = None,
        key_reader: Callable[[], str | TerminalKey] | None = None,
    ) -> None:
        self.commands = commands
        self.input = input_stream or sys.stdin
        self.output = output_stream or sys.stdout
        self._key_reader = key_reader
        if interactive is None:
            input_tty = bool(getattr(self.input, "isatty", lambda: False)())
            output_tty = bool(getattr(self.output, "isatty", lambda: False)())
            interactive = input_tty and output_tty
        self.interactive = interactive

    def readline(self, prompt: str) -> str:
        """Return one line, opening the command selector as soon as `/` is typed."""

        self._write(prompt, flush=True)
        if not self.interactive:
            return self.input.readline()
        with self._input_mode():
            return self._read_edited_line(prompt)

    def select(
        self,
        title: str,
        choices: tuple[CommandChoice, ...],
    ) -> str | None:
        """Select one value, with a line-mode fallback for redirected input."""

        if not choices:
            return None
        if not self.interactive:
            width = max(len(choice.command) for choice in choices)
            self._write(f"{title}:\n")
            for choice in choices:
                self._write(
                    f"  {choice.command:<{width}}  {choice.description}\n"
                )
            self._write("Session reference (blank to cancel): ", flush=True)
            value = self.input.readline().strip()
            return value or None

        self._write(f"{title}:", flush=True)
        with self._input_mode():
            selection = self._select_choices(
                title=title,
                choices=choices,
                query_prefix="",
                enter_action="select",
                cancel_message="Selection cancelled.",
                search_descriptions=True,
            )
        return selection if isinstance(selection, str) else None

    def _read_edited_line(self, prompt: str) -> str:
        buffer: list[str] = []
        cursor = 0
        rendered_width = 0
        while True:
            key = self._next_key()
            if key is TerminalKey.INTERRUPT:
                raise KeyboardInterrupt
            if key is TerminalKey.EOF:
                if not buffer:
                    return ""
                continue
            if key is TerminalKey.ENTER:
                self._write("\n", flush=True)
                return "".join(buffer) + "\n"
            if key is TerminalKey.BACKSPACE:
                if cursor > 0:
                    del buffer[cursor - 1]
                    cursor -= 1
            elif key is TerminalKey.DELETE:
                if cursor < len(buffer):
                    del buffer[cursor]
            elif key is TerminalKey.LEFT:
                cursor = max(0, cursor - 1)
            elif key is TerminalKey.RIGHT:
                cursor = min(len(buffer), cursor + 1)
            elif key is TerminalKey.HOME:
                cursor = 0
            elif key is TerminalKey.END:
                cursor = len(buffer)
            elif isinstance(key, str) and _is_printable(key):
                if key == "/" and not buffer:
                    self._write("/", flush=True)
                    selection = self._select_command()
                    if selection is TerminalKey.EOF:
                        return ""
                    if selection is TerminalKey.ESCAPE:
                        return "\n"
                    buffer = list(selection)
                    cursor = len(buffer)
                    rendered_width = self._redraw_line(
                        prompt,
                        buffer,
                        cursor,
                        rendered_width,
                    )
                    continue
                buffer.insert(cursor, key)
                cursor += 1
            else:
                continue
            rendered_width = self._redraw_line(
                prompt,
                buffer,
                cursor,
                rendered_width,
            )

    def _select_command(self) -> str | TerminalKey:
        return self._select_choices(
            title="Commands",
            choices=self.commands,
            query_prefix="/",
            enter_action="complete",
            cancel_message="Command menu cancelled.",
            search_descriptions=False,
        )

    def _select_choices(
        self,
        *,
        title: str,
        choices: tuple[CommandChoice, ...],
        query_prefix: str,
        enter_action: str,
        cancel_message: str,
        search_descriptions: bool,
    ) -> str | TerminalKey:
        query = ""
        selected = 0
        menu_lines = max(1, len(choices)) + 2
        self._write("\n")
        self._render_choice_menu(
            title=title,
            choices=choices,
            query=query,
            query_prefix=query_prefix,
            selected=selected,
            menu_lines=menu_lines,
            enter_action=enter_action,
            search_descriptions=search_descriptions,
            redraw=False,
        )
        while True:
            key = self._next_key()
            matches = self._matching_choices(
                choices,
                query,
                query_prefix=query_prefix,
                search_descriptions=search_descriptions,
            )
            if key is TerminalKey.INTERRUPT:
                raise KeyboardInterrupt
            if key is TerminalKey.EOF:
                self._close_command_menu(menu_lines)
                self._write("\n", flush=True)
                return TerminalKey.EOF
            if key is TerminalKey.ESCAPE:
                self._close_command_menu(menu_lines)
                self._write(cancel_message + "\n", flush=True)
                return TerminalKey.ESCAPE
            if key is TerminalKey.BACKSPACE:
                if query:
                    query = query[:-1]
                    selected = 0
                else:
                    self._close_command_menu(menu_lines)
                    self._write(cancel_message + "\n", flush=True)
                    return TerminalKey.ESCAPE
            elif key in {TerminalKey.DOWN, TerminalKey.TAB}:
                if matches:
                    selected = (selected + 1) % len(matches)
            elif key is TerminalKey.UP:
                if matches:
                    selected = (selected - 1) % len(matches)
            elif key is TerminalKey.ENTER:
                if matches:
                    command = matches[selected].command
                else:
                    command = query_prefix + query
                self._close_command_menu(menu_lines)
                return command
            elif isinstance(key, str) and _is_printable(key):
                query += key.lower()
                selected = 0
            else:
                continue

            matches = self._matching_choices(
                choices,
                query,
                query_prefix=query_prefix,
                search_descriptions=search_descriptions,
            )
            if matches:
                selected %= len(matches)
            else:
                selected = 0
            self._render_choice_menu(
                title=title,
                choices=choices,
                query=query,
                query_prefix=query_prefix,
                selected=selected,
                menu_lines=menu_lines,
                enter_action=enter_action,
                search_descriptions=search_descriptions,
                redraw=True,
            )

    @staticmethod
    def _matching_choices(
        choices: tuple[CommandChoice, ...],
        query: str,
        *,
        query_prefix: str,
        search_descriptions: bool,
    ) -> list[CommandChoice]:
        prefix = (query_prefix + query).casefold()
        needle = query.casefold()
        return [
            choice
            for choice in choices
            if choice.command.casefold().startswith(prefix)
            or (
                search_descriptions
                and needle
                and needle in choice.description.casefold()
            )
        ]

    def _render_choice_menu(
        self,
        *,
        title: str,
        choices: tuple[CommandChoice, ...],
        query: str,
        query_prefix: str,
        selected: int,
        menu_lines: int,
        enter_action: str,
        search_descriptions: bool,
        redraw: bool,
    ) -> None:
        matches = self._matching_choices(
            choices,
            query,
            query_prefix=query_prefix,
            search_descriptions=search_descriptions,
        )
        width = max((len(choice.command) for choice in choices), default=0)
        rows = [
            f"{title} (↑/↓ select, Enter {enter_action}, type to filter, "
            "Esc cancel):"
        ]
        if matches:
            for index, choice in enumerate(matches):
                command = f"{choice.command:<{width}}"
                if index == selected:
                    command = f"\x1b[7m{command}\x1b[0m"
                rows.append(f"  {command}  {choice.description}")
        else:
            rows.append("  No matching command")
        while len(rows) < menu_lines - 1:
            rows.append("")
        rows.append(f"Filter: {query_prefix}{query}")

        if redraw:
            self._write(f"\x1b[{menu_lines}A")
        for row in rows:
            self._write("\r\x1b[2K" + row + "\n")
        self.output.flush()

    def _close_command_menu(self, menu_lines: int) -> None:
        self._write(f"\x1b[{menu_lines + 1}A")
        for index in range(menu_lines + 1):
            self._write("\r\x1b[2K")
            if index < menu_lines:
                self._write("\x1b[1B")
        if menu_lines:
            self._write(f"\x1b[{menu_lines}A")
        self._write("\r", flush=True)

    def _redraw_line(
        self,
        prompt: str,
        buffer: list[str],
        cursor: int,
        previous_width: int,
    ) -> int:
        value = "".join(buffer)
        width = _display_width(value)
        padding = " " * max(0, previous_width - width)
        self._write("\r" + prompt + value + padding)
        self._write("\r" + prompt + "".join(buffer[:cursor]), flush=True)
        return width

    def _next_key(self) -> str | TerminalKey:
        if self._key_reader is not None:
            try:
                return _normalize_key(self._key_reader())
            except StopIteration:
                return TerminalKey.EOF
        if os.name == "nt":
            return self._read_windows_key()
        return self._read_posix_key()

    @staticmethod
    def _read_windows_key() -> str | TerminalKey:
        import msvcrt

        value = msvcrt.getwch()
        if value in {"\x00", "\xe0"}:
            code = msvcrt.getwch()
            return {
                "H": TerminalKey.UP,
                "P": TerminalKey.DOWN,
                "K": TerminalKey.LEFT,
                "M": TerminalKey.RIGHT,
                "G": TerminalKey.HOME,
                "O": TerminalKey.END,
                "S": TerminalKey.DELETE,
            }.get(code, TerminalKey.ESCAPE)
        if value == "\x1b" and msvcrt.kbhit():
            sequence = value
            while msvcrt.kbhit() and len(sequence) < 4:
                sequence += msvcrt.getwch()
            return _normalize_key(sequence)
        return _normalize_key(value)

    def _read_posix_key(self) -> str | TerminalKey:
        value = self.input.read(1)
        if value != "\x1b":
            return _normalize_key(value)
        sequence = value
        try:
            import select

            for _ in range(3):
                ready, _, _ = select.select([self.input], [], [], 0.03)
                if not ready:
                    break
                sequence += self.input.read(1)
        except (OSError, ValueError):
            pass
        return _normalize_key(sequence)

    @contextmanager
    def _input_mode(self) -> Iterator[None]:
        if self._key_reader is not None or os.name == "nt":
            yield
            return
        try:
            import termios
            import tty
        except ImportError:
            yield
            return
        try:
            descriptor = self.input.fileno()
            previous = termios.tcgetattr(descriptor)
            tty.setraw(descriptor)
        except (AttributeError, OSError):
            yield
            return
        try:
            yield
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)

    def _write(self, value: str, *, flush: bool = False) -> None:
        self.output.write(value)
        if flush:
            self.output.flush()


def _normalize_key(value: str | TerminalKey) -> str | TerminalKey:
    if isinstance(value, TerminalKey):
        return value
    special = {
        "": TerminalKey.EOF,
        "\r": TerminalKey.ENTER,
        "\n": TerminalKey.ENTER,
        "\x08": TerminalKey.BACKSPACE,
        "\x7f": TerminalKey.BACKSPACE,
        "\x03": TerminalKey.INTERRUPT,
        "\x04": TerminalKey.EOF,
        "\x1a": TerminalKey.EOF,
        "\t": TerminalKey.TAB,
        "\x1b": TerminalKey.ESCAPE,
        "\x1b[A": TerminalKey.UP,
        "\x1b[B": TerminalKey.DOWN,
        "\x1b[C": TerminalKey.RIGHT,
        "\x1b[D": TerminalKey.LEFT,
        "\x1b[H": TerminalKey.HOME,
        "\x1b[F": TerminalKey.END,
        "\x1b[3~": TerminalKey.DELETE,
    }
    return special.get(value, value)


def _is_printable(value: str) -> bool:
    return len(value) == 1 and value.isprintable()


def _display_width(value: str) -> int:
    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width
