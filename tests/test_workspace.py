from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_agent.workspace import WorkspaceSetupError, prepare_workspace


_TEST_IDENTITY = {
    "GIT_AUTHOR_NAME": "Workspace Test",
    "GIT_AUTHOR_EMAIL": "workspace-test@example.invalid",
    "GIT_COMMITTER_NAME": "Workspace Test",
    "GIT_COMMITTER_EMAIL": "workspace-test@example.invalid",
}


class WorkspaceSetupTests(unittest.TestCase):
    def test_non_git_directory_gets_ignore_rules_and_initial_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "app.py").write_text("print('hello')\n", encoding="utf-8")
            (workspace / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (workspace / ".env").write_text("TOKEN=not-a-real-secret\n", encoding="utf-8")
            (workspace / "id_rsa").write_text("not-a-real-key\n", encoding="utf-8")
            cache = workspace / "__pycache__"
            cache.mkdir()
            (cache / "app.pyc").write_bytes(b"cache")

            with patch.dict(os.environ, _TEST_IDENTITY):
                result = prepare_workspace(workspace)

            self.assertTrue(result.initialized)
            self.assertTrue(result.gitignore_updated)
            self.assertIsNotNone(result.initial_commit)
            self.assertTrue((workspace / ".git").exists())
            self.assertEqual(self._git(workspace, "status", "--porcelain"), "")
            self.assertEqual(
                self._git(workspace, "log", "-1", "--format=%s"),
                "chore: initialize repository",
            )

            tracked = set(self._git(workspace, "ls-files").splitlines())
            self.assertIn(".gitignore", tracked)
            self.assertIn("app.py", tracked)
            self.assertIn("pyproject.toml", tracked)
            self.assertNotIn(".env", tracked)
            self.assertNotIn("id_rsa", tracked)
            self.assertNotIn("__pycache__/app.pyc", tracked)

            ignore = (workspace / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("# Added by coding-agent workspace setup", ignore)
            self.assertIn(".env", ignore)
            self.assertIn("!.env.example", ignore)
            self.assertIn("*.pem", ignore)
            self.assertIn("__pycache__/", ignore)

    def test_existing_gitignore_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            original = "# Project rule\n*.local-output\n.env\n!.env\n"
            (workspace / ".gitignore").write_text(original, encoding="utf-8")
            (workspace / "app.txt").write_text("tracked\n", encoding="utf-8")
            (workspace / "scratch.local-output").write_text("ignored\n", encoding="utf-8")
            (workspace / ".env").write_text("TOKEN=not-a-real-secret\n", encoding="utf-8")

            with patch.dict(os.environ, _TEST_IDENTITY):
                result = prepare_workspace(workspace)

            self.assertTrue(result.gitignore_updated)
            updated = (workspace / ".gitignore").read_text(encoding="utf-8")
            self.assertTrue(updated.startswith(original))
            self.assertEqual(updated.count("# Added by coding-agent workspace setup"), 1)
            tracked = set(self._git(workspace, "ls-files").splitlines())
            self.assertNotIn("scratch.local-output", tracked)
            self.assertNotIn(".env", tracked)

    def test_existing_repository_is_a_strict_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            self._git(workspace, "init", "-q")
            self._git(workspace, "config", "user.name", "Existing User")
            self._git(workspace, "config", "user.email", "existing@example.invalid")
            (workspace / "tracked.txt").write_text("baseline\n", encoding="utf-8")
            self._git(workspace, "add", "tracked.txt")
            self._git(workspace, "commit", "-q", "-m", "baseline")
            before_head = self._git(workspace, "rev-parse", "HEAD")

            result = prepare_workspace(workspace)

            self.assertFalse(result.initialized)
            self.assertFalse(result.gitignore_updated)
            self.assertIsNone(result.initial_commit)
            self.assertEqual(self._git(workspace, "rev-parse", "HEAD"), before_head)
            self.assertFalse((workspace / ".gitignore").exists())

    def test_subdirectory_of_existing_repository_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            self._git(parent, "init", "-q")
            nested = parent / "nested"
            nested.mkdir()

            with self.assertRaisesRegex(WorkspaceSetupError, "inside Git repository"):
                prepare_workspace(nested)

            self.assertFalse((nested / ".git").exists())
            self.assertFalse((nested / ".gitignore").exists())

    def test_invalid_git_entry_is_not_reinitialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".git").write_text("invalid\n", encoding="utf-8")

            with self.assertRaisesRegex(WorkspaceSetupError, "not usable"):
                prepare_workspace(workspace)

            self.assertEqual((workspace / ".git").read_text(encoding="utf-8"), "invalid\n")

    @staticmethod
    def _git(workspace: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
