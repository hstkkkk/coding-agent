from __future__ import annotations

import io
import unittest

from coding_agent.terminal import CommandChoice, TerminalKey, TerminalPrompt


COMMANDS = (
    CommandChoice("/help", "Show this help."),
    CommandChoice("/workspace", "Show the active repository root."),
    CommandChoice("/history", "Show runs from this terminal session."),
    CommandChoice("/clear", "Clear the terminal."),
    CommandChoice("/exit", "End the session."),
)


class TerminalPromptTests(unittest.TestCase):
    def test_slash_opens_menu_and_arrows_select_a_command(self) -> None:
        output = io.StringIO()
        prompt = self._prompt(
            [
                "/",
                TerminalKey.DOWN,
                TerminalKey.DOWN,
                TerminalKey.ENTER,
                TerminalKey.ENTER,
            ],
            output,
        )

        result = prompt.readline("coding-agent> ")

        self.assertEqual(result, "/history\n")
        rendered = output.getvalue()
        self.assertIn("Commands", rendered)
        self.assertIn("/help", rendered)
        self.assertIn("/workspace", rendered)
        self.assertIn("\x1b[7m/history", rendered)
        self.assertNotIn("Selected:", rendered)
        self.assertIn("↑/↓", rendered)

    def test_enter_completes_selection_without_submitting_the_line(self) -> None:
        output = io.StringIO()
        prompt = self._prompt(
            [
                "/",
                TerminalKey.DOWN,
                TerminalKey.DOWN,
                TerminalKey.ENTER,
                " ",
                "x",
                TerminalKey.ENTER,
            ],
            output,
        )

        result = prompt.readline("coding-agent> ")

        self.assertEqual(result, "/history x\n")
        self.assertIn("coding-agent> /history", output.getvalue())

    def test_select_reuses_highlighted_menu_for_session_choices(self) -> None:
        output = io.StringIO()
        prompt = self._prompt([TerminalKey.DOWN, TerminalKey.ENTER], output)
        choices = (
            CommandChoice("ae9ad21e", "11:45 · no completed request"),
            CommandChoice("93df3b75", "11:35 · last: Fix parser tests"),
        )

        result = prompt.select("Resume session", choices)

        self.assertEqual(result, "93df3b75")
        self.assertIn("\x1b[7m93df3b75", output.getvalue())
        self.assertIn("Fix parser tests", output.getvalue())

    def test_typing_filters_the_menu_and_backspace_edits_filter(self) -> None:
        output = io.StringIO()
        prompt = self._prompt(
            [
                "/",
                "h",
                "e",
                TerminalKey.BACKSPACE,
                "i",
                TerminalKey.ENTER,
                TerminalKey.ENTER,
            ],
            output,
        )

        result = prompt.readline("coding-agent> ")

        self.assertEqual(result, "/history\n")
        self.assertIn("Filter: /hi", output.getvalue())

    def test_regular_unicode_input_still_works(self) -> None:
        output = io.StringIO()
        prompt = self._prompt(
            ["写", "一", "个", "游", "戏", TerminalKey.ENTER],
            output,
        )

        result = prompt.readline("coding-agent> ")

        self.assertEqual(result, "写一个游戏\n")

    def test_long_unicode_input_is_echoed_once_without_full_line_redraws(self) -> None:
        output = io.StringIO()
        request = "把沙子的质感、流动效果和交互细节全部修复，并保持现有功能。" * 8
        prompt = self._prompt([*request, TerminalKey.ENTER], output)

        result = prompt.readline("coding-agent> ")

        self.assertEqual(result, request + "\n")
        self.assertEqual(output.getvalue(), "coding-agent> " + request + "\n")

    def test_escape_or_empty_filter_backspace_cancels_the_menu(self) -> None:
        for key in (TerminalKey.ESCAPE, TerminalKey.BACKSPACE):
            with self.subTest(key=key):
                output = io.StringIO()
                prompt = self._prompt(["/", key], output)

                result = prompt.readline("coding-agent> ")

                self.assertEqual(result, "\n")
                self.assertIn("Command menu cancelled.", output.getvalue())

    def test_line_editor_supports_cursor_movement_and_delete(self) -> None:
        output = io.StringIO()
        prompt = self._prompt(
            [
                "a",
                "c",
                TerminalKey.LEFT,
                "b",
                TerminalKey.RIGHT,
                "d",
                TerminalKey.LEFT,
                TerminalKey.DELETE,
                TerminalKey.ENTER,
            ],
            output,
        )

        self.assertEqual(prompt.readline("coding-agent> "), "abc\n")

    def test_line_mode_remains_available_for_redirected_input(self) -> None:
        output = io.StringIO()
        prompt = TerminalPrompt(
            commands=COMMANDS,
            input_stream=io.StringIO("/help\n"),
            output_stream=output,
            interactive=False,
        )

        self.assertEqual(prompt.readline("coding-agent> "), "/help\n")
        self.assertEqual(output.getvalue(), "coding-agent> ")

    @staticmethod
    def _prompt(keys: list[str | TerminalKey], output: io.StringIO) -> TerminalPrompt:
        iterator = iter(keys)
        return TerminalPrompt(
            commands=COMMANDS,
            input_stream=io.StringIO(),
            output_stream=output,
            interactive=True,
            key_reader=lambda: next(iterator),
        )


if __name__ == "__main__":
    unittest.main()
