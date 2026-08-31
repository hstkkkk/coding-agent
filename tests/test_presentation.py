from __future__ import annotations

import unittest

from coding_agent.presentation import describe_tool, describe_tool_result


class ToolPresentationTests(unittest.TestCase):
    def test_file_actions_name_their_target_without_exposing_content(self) -> None:
        self.assertEqual(
            describe_tool(
                "list_directory",
                {"path": "src", "recursive": True},
            ),
            "path=src · recursive",
        )
        self.assertEqual(
            describe_tool(
                "read_file",
                {"path": "index.html", "start_line": 10, "end_line": 20},
            ),
            "path=index.html · lines=10-20",
        )
        summary = describe_tool(
            "create_file",
            {"path": "index.html", "content": "private-source-marker"},
        )
        self.assertEqual(summary, "path=index.html · 21 chars")
        self.assertNotIn("private-source-marker", summary)

    def test_command_action_is_bounded_and_identifies_inline_code(self) -> None:
        inline_code = "private-inline-marker" * 500

        summary = describe_tool(
            "run_command",
            {
                "program": "node",
                "args": ["-e", inline_code],
                "cwd": ".",
                "purpose": "verify",
            },
        )

        self.assertEqual(
            summary,
            "program=node · cwd=. · purpose=verify · 2 args · "
            f"inline code={len(inline_code)} chars",
        )
        self.assertNotIn("private-inline-marker", summary)

    def test_results_include_the_observable_outcome(self) -> None:
        listed = describe_tool_result(
            "list_directory",
            {"path": "src"},
            {"entries": [{"path": "src/a.py"}, {"path": "src/b.py"}]},
        )
        command = describe_tool_result(
            "run_command",
            {"program": "python", "args": ["-m", "unittest"]},
            {"exit_code": 0, "workspace_changed": False},
        )
        created = describe_tool_result(
            "create_file",
            {"path": "app.py", "content": "print('ok')"},
            {"workspace_changed": True, "changed_files": ["app.py"]},
        )

        self.assertEqual(listed, "path=src · 2 entries")
        self.assertEqual(command, "program=python · exit=0")
        self.assertEqual(created, "path=app.py · changed")


if __name__ == "__main__":
    unittest.main()
