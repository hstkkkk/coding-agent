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


if __name__ == "__main__":
    unittest.main()
