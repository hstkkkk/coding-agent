from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_agent.settings import (
    SettingsError,
    default_settings_path,
    load_user_settings,
)


class UserSettingsTests(unittest.TestCase):
    def test_default_path_is_in_the_per_user_coding_agent_directory(self) -> None:
        path = default_settings_path()

        self.assertEqual(path.name, "settings.json")
        self.assertEqual(path.parent.name, ".coding-agent")
        self.assertTrue(path.is_absolute())

    def test_missing_file_returns_empty_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_user_settings(Path(directory) / "missing.json")

        self.assertIsNone(settings.api_key)
        self.assertIsNone(settings.model)
        self.assertEqual(settings.allow_programs, ())

    def test_no_argument_loads_from_the_fixed_home_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config_directory = home / ".coding-agent"
            config_directory.mkdir()
            (config_directory / "settings.json").write_text(
                '{"model":"fixed-path-model"}',
                encoding="utf-8",
            )

            with patch("coding_agent.settings.Path.home", return_value=home):
                settings = load_user_settings()

        self.assertEqual(settings.model, "fixed-path-model")

    def test_loads_and_normalizes_complete_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "api_key": "test-secret-value",
                        "api_key_env": "ALTERNATE_API_KEY",
                        "model": "deepseek-v4-flash",
                        "base_url": "https://api.deepseek.com",
                        "thinking": None,
                        "max_turns": 40,
                        "max_seconds": 1200,
                        "command_timeout": 180,
                        "approval_mode": "deny",
                        "allow_programs": ["Python", "python", "git.exe"],
                        "runs_dir": "run-data",
                    }
                ),
                encoding="utf-8",
            )

            settings = load_user_settings(path)

        self.assertEqual(settings.api_key, "test-secret-value")
        self.assertEqual(settings.api_key_env, "ALTERNATE_API_KEY")
        self.assertEqual(settings.model, "deepseek-v4-flash")
        self.assertEqual(settings.base_url, "https://api.deepseek.com")
        self.assertIsNone(settings.thinking)
        self.assertTrue(settings.provides("thinking"))
        self.assertEqual(settings.max_turns, 40)
        self.assertEqual(settings.max_seconds, 1200)
        self.assertEqual(settings.command_timeout, 180)
        self.assertEqual(settings.approval_mode, "deny")
        self.assertEqual(settings.allow_programs, ("Python", "git.exe"))
        self.assertEqual(settings.runs_dir, (root / "run-data").resolve())
        self.assertNotIn("test-secret-value", repr(settings))

    def test_malformed_json_does_not_echo_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"api_key": "do-not-echo",', encoding="utf-8")

            with self.assertRaises(SettingsError) as raised:
                load_user_settings(path)

        self.assertNotIn("do-not-echo", str(raised.exception))
        self.assertIn("valid JSON", str(raised.exception))

    def test_rejects_non_object_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(SettingsError, "JSON object"):
                load_user_settings(path)

            path.write_text('{"surprise": true}', encoding="utf-8")
            with self.assertRaisesRegex(SettingsError, "unknown settings field: surprise"):
                load_user_settings(path)

    def test_rejects_invalid_field_values(self) -> None:
        invalid_values = {
            "api_key": 123,
            "api_key_env": "NOT-AN-ENV-NAME",
            "model": "   ",
            "base_url": "not-a-url",
            "thinking": "automatic",
            "max_turns": True,
            "max_seconds": 0,
            "command_timeout": 601,
            "approval_mode": "ask",
            "allow_programs": ["../python"],
            "runs_dir": "",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            for field, value in invalid_values.items():
                with self.subTest(field=field):
                    path.write_text(json.dumps({field: value}), encoding="utf-8")
                    with self.assertRaisesRegex(SettingsError, field):
                        load_user_settings(path)

    def test_rejects_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_bytes(b" " * (64 * 1024 + 1))

            with self.assertRaisesRegex(SettingsError, "size limit"):
                load_user_settings(path)


if __name__ == "__main__":
    unittest.main()
