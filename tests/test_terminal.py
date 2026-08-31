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
            ["/", TerminalKey.DOWN, TerminalKey.DOWN, TerminalKey.ENTER],
            output,
        )

        result = prompt.readline("coding-agent> ")

        self.assertEqual(result, "/history\n")
        rendered = output.getvalue()
        self.assertIn("Commands", rendered)
        self.assertIn("/help", rendered)
        self.assertIn("/workspace", rendered)
        self.assertIn("Selected: /history", rendered)
        self.assertIn("↑/↓", rendered)

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
