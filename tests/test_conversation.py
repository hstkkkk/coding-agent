from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coding_agent.conversation import (
    ConversationError,
    ConversationLimits,
    ConversationStore,
)
from coding_agent.domain import RunResult, RunStatus, VerificationRecord
from coding_agent.events import Redactor


class ConversationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.sessions_root = root / "sessions"
        self.workspace = root / "workspace"
        self.workspace.mkdir()

    def test_resume_restores_both_sides_of_completed_turns(self) -> None:
        store = ConversationStore(self.sessions_root, Redactor())
        session = store.create(self.workspace)
        session.record("Who are you?", self._result(1, "I am a coding agent."))

        resumed = store.resume(session.session_id, self.workspace)
        prepared = resumed.prepare("What did you just say?")

        self.assertTrue(resumed.resumed)
        self.assertIn("Who are you?", prepared.objective)
        self.assertIn("I am a coding agent.", prepared.objective)
        self.assertIn("Current request:\nWhat did you just say?", prepared.objective)
        self.assertEqual(resumed.history().total_turns, 1)

    def test_resume_is_bound_to_the_original_workspace(self) -> None:
        store = ConversationStore(self.sessions_root, Redactor())
        session = store.create(self.workspace)
        other_workspace = Path(self.temporary.name) / "other"
        other_workspace.mkdir()

        with self.assertRaisesRegex(ConversationError, "different workspace"):
            store.resume(session.session_id, other_workspace)
        with self.assertRaisesRegex(ConversationError, "session id"):
            store.resume("../outside", self.workspace)

    def test_persistence_redacts_secrets_and_omits_verification_evidence(self) -> None:
        secret = "local-test-secret"
        verification_id = "v" * 32
        store = ConversationStore(self.sessions_root, Redactor([secret]))
        session = store.create(self.workspace)
        result = RunResult(
            run_id="1" * 32,
            status=RunStatus.SUCCEEDED,
            summary=f"completed without echoing {secret}",
            changed_files=("app.py",),
            verifications=(
                VerificationRecord(
                    verification_id=verification_id,
                    command=("python", "-m", "unittest"),
                    exit_code=0,
                    workspace_version=1,
                    passed=True,
                ),
            ),
        )

        session.record(f"use {secret}", result)

        encoded = (
            self.sessions_root / session.session_id / "session.jsonl"
        ).read_text(encoding="utf-8")
        self.assertNotIn(secret, encoded)
        self.assertNotIn(verification_id, encoded)
        self.assertIn("REDACTED", encoded)

    def test_prepare_automatically_compacts_old_turns_and_persists_memory(self) -> None:
        limits = ConversationLimits(
            max_context_chars=1_600,
            target_context_chars=900,
            min_recent_turns=2,
            max_memory_digests=4,
        )
        store = ConversationStore(self.sessions_root, Redactor(), limits=limits)
        session = store.create(self.workspace)
        for index in range(1, 9):
            session.record(
                f"request-{index} " + ("x" * 260),
                self._result(index, f"response-{index} " + ("y" * 360)),
            )

        prepared = session.prepare("continue")
        history = session.history()

        self.assertGreater(prepared.compacted_now, 0)
        self.assertLessEqual(prepared.context_chars, limits.max_context_chars)
        self.assertIn("<compacted_memory", prepared.objective)
        self.assertIn("request-8", prepared.objective)
        self.assertGreater(history.compacted_turns, 0)
        resumed = store.resume(session.session_id, self.workspace)
        self.assertEqual(resumed.history().compacted_turns, history.compacted_turns)
        self.assertIn("<compacted_memory", resumed.prepare("again").objective)

    def test_lists_recent_sessions_without_reading_model_configuration(self) -> None:
        store = ConversationStore(self.sessions_root, Redactor())
        first = store.create(self.workspace)
        first.record("first", self._result(1, "done"))
        second = store.create(self.workspace)

        sessions = store.list_sessions(workspace=self.workspace)

        self.assertEqual(
            {item.session_id for item in sessions},
            {first.session_id, second.session_id},
        )
        first_info = next(item for item in sessions if item.session_id == first.session_id)
        self.assertEqual(first_info.turn_count, 1)
        self.assertEqual(first_info.workspace, self.workspace.resolve())

    def test_current_request_is_bounded_even_without_prior_history(self) -> None:
        store = ConversationStore(self.sessions_root, Redactor())
        session = store.create(self.workspace)

        prepared = session.prepare("start-" + "x" * 40_000 + "-end")

        self.assertLessEqual(len(prepared.objective), 19_000)
        self.assertTrue(prepared.objective.startswith("start-"))
        self.assertTrue(prepared.objective.endswith("-end"))
        self.assertIn("...[compacted]...", prepared.objective)

    @staticmethod
    def _result(index: int, summary: str) -> RunResult:
        return RunResult(
            run_id=f"{index:x}" * 32,
            status=RunStatus.ANSWERED,
            summary=summary,
            changed_files=(),
            verifications=(),
        )


if __name__ == "__main__":
    unittest.main()
