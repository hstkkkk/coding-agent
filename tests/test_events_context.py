from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from coding_agent.artifacts import ArtifactStore
from coding_agent.context import ContextManager
from coding_agent.domain import RunEvent, RunOptions, RunState
from coding_agent.events import ConsoleEventSink, JsonlEventSink, Redactor


class EventAndContextTests(unittest.TestCase):
    def test_console_progress_uses_structured_detail_without_empty_colons(self) -> None:
        output = io.StringIO()
        sink = ConsoleEventSink()

        with redirect_stdout(output):
            sink.emit(
                RunEvent(
                    run_id="run",
                    sequence=1,
                    kind="model_action",
                    timestamp="now",
                    data={
                        "action": "list_directory",
                        "detail": "path=src · recursive",
                        "rationale": "",
                        "repeated": 2,
                    },
                )
            )
            sink.emit(
                RunEvent(
                    run_id="run",
                    sequence=2,
                    kind="tool_finished",
                    timestamp="now",
                    data={
                        "tool": "list_directory",
                        "detail": "path=src · 3 entries",
                        "status": "COMPLETED",
                        "duration_ms": 4,
                    },
                )
            )

        rendered = output.getvalue()
        self.assertIn(
            "[MODEL] list_directory · path=src · recursive · repeated 2x",
            rendered,
        )
        self.assertIn(
            "[TOOL] list_directory · path=src · 3 entries -> COMPLETED (4 ms)",
            rendered,
        )
        self.assertNotIn("list_directory:", rendered)

    def test_console_renders_answered_terminal_as_an_answer(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            ConsoleEventSink().emit(
                RunEvent(
                    run_id="run",
                    sequence=1,
                    kind="terminal",
                    timestamp="now",
                    data={"status": "ANSWERED", "summary": "I am a coding agent."},
                )
            )

        self.assertEqual(output.getvalue(), "[ANSWER] I am a coding agent.\n")

    def test_console_reports_how_to_restore_a_before_image(self) -> None:
        output = io.StringIO()
        recovery_id = "b" * 32

        with redirect_stdout(output):
            ConsoleEventSink().emit(
                RunEvent(
                    run_id="a" * 32,
                    sequence=1,
                    kind="tool_finished",
                    timestamp="now",
                    data={
                        "tool": "write_file",
                        "detail": "path=index.html · changed",
                        "status": "COMPLETED",
                        "duration_ms": 4,
                        "recovery_output_id": recovery_id,
                        "recovery_path": "index.html",
                    },
                )
            )

        rendered = output.getvalue()
        self.assertIn("[RECOVERY]", rendered)
        self.assertIn(
            f"coding-agent recover-file {'a' * 32} {recovery_id}",
            rendered,
        )
        self.assertIn("index.html", rendered)

    def test_redacts_known_and_pattern_secrets_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            secret = "known-secret-value"
            fake_token = "sk-" + "abcdefghijk"
            sink = JsonlEventSink(path, Redactor([secret]))
            sink.emit(
                RunEvent(
                    run_id="run",
                    sequence=1,
                    kind="test",
                    timestamp="now",
                    data={"a": secret, "b": "api_key=visible-value", "c": fake_token},
                )
            )

            encoded = path.read_text(encoding="utf-8")

            self.assertNotIn(secret, encoded)
            self.assertNotIn("visible-value", encoded)
            self.assertNotIn(fake_token, encoded)
            self.assertIn("REDACTED", encoded)
            json.loads(encoded)

    def test_jsonl_replaces_unpaired_surrogates_before_utf8_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            sink = JsonlEventSink(path, Redactor())
            malformed = "broken" + chr(0xDC81)

            sink.emit(
                RunEvent(
                    run_id="run",
                    sequence=1,
                    kind="test",
                    timestamp="now",
                    data={"text": malformed},
                )
            )

            encoded = path.read_text(encoding="utf-8")
            self.assertNotIn(chr(0xDC81), encoded)
            self.assertIn("\N{REPLACEMENT CHARACTER}", encoded)
            json.loads(encoded)

    def test_artifact_ids_cannot_escape_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), Redactor())
            reference = store.write_text("hello")
            content, total = store.read_text(reference.output_id, 0, 10)
            self.assertEqual((content, total), ("hello", 5))
            with self.assertRaises(ValueError):
                store.read_text("../outside", 0, 10)

    def test_context_preserves_objective_and_latest_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = RunState("run", "fix parser", Path(directory))
            state.recent_errors.extend(["old", "latest"])
            manager = ContextManager(RunOptions(max_context_chars=4_000))

            request = manager.build(state, ())

            self.assertIn("fix parser", request.user_prompt)
            self.assertIn("latest", request.user_prompt)
            self.assertIn("controller", request.system_prompt.lower())
            normalized_prompt = " ".join(request.system_prompt.split())
            self.assertIn("call respond immediately", normalized_prompt)


if __name__ == "__main__":
    unittest.main()
