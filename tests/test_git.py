from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from coding_agent.tools.git import GitInspector


class GitInspectorTests(unittest.TestCase):
    def test_working_tree_diff_includes_untracked_text_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Candidate"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "candidate@example.invalid"],
                cwd=workspace,
                check=True,
            )
            (workspace / ".gitignore").write_text(".env\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "fixture"],
                cwd=workspace,
                check=True,
            )
            (workspace / "index.html").write_text(
                "<main>sand game</main>\n",
                encoding="utf-8",
            )

            rendered = GitInspector(workspace).diff()

        self.assertIn("diff --git a/index.html b/index.html", rendered)
        self.assertIn("+++ b/index.html", rendered)
        self.assertIn("+<main>sand game</main>", rendered)


if __name__ == "__main__":
    unittest.main()
