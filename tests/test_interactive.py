from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from coding_agent.conversation import ConversationStore
from coding_agent.domain import RunResult, RunStatus
from coding_agent.events import Redactor
from coding_agent.interactive import InteractiveSession


class InteractiveSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.store = ConversationStore(root / "sessions", Redactor())

    def test_runs_multiple_requests_with_persistent_session_context(self) -> None:
        source = io.StringIO(
            "task one\n"
            "task two\n"
            "task three\n"
            "task four\n"
            "task five\n"
            "task six\n"
            "task seven\n"
            "task eight\n"
            "/history\n"
            "/exit\n"
        )
        output = io.StringIO()
        objectives: list[str] = []

        def run_task(objective: str) -> RunResult:
            objectives.append(objective)
            index = len(objectives)
            return self._result(index)

        session = InteractiveSession(
            conversation=self.store.create(self.workspace),
            model_label="test-model",
            input_stream=source,
            output_stream=output,
            styled=False,
        )

        exit_code = session.run(run_task)

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(objectives), 8)
        self.assertEqual(objectives[0], "task one")
        self.assertIn("Current request:\ntask eight", objectives[-1])
        self.assertIn("task one", objectives[-1])
        self.assertIn("task two", objectives[-1])
        self.assertIn("summary-7", objectives[-1])
        rendered = output.getvalue()
        self.assertIn("Bounded Coding Agent", rendered)
        self.assertIn("Session:", rendered)
        self.assertIn("Type / to browse commands", rendered)
        self.assertIn("SUCCEEDED", rendered)
        self.assertIn("8" * 32, rendered)

    def test_slash_commands_do_not_invoke_runner(self) -> None:
        source = io.StringIO(
            "/help\n/workspace\n/session\n/history\n/clear\n/unknown\n/quit\n"
        )
        output = io.StringIO()

        def unexpected(_: str) -> RunResult:
            self.fail("slash command invoked the task runner")

        session = InteractiveSession(
            conversation=self.store.create(self.workspace),
            model_label="test-model",
            input_stream=source,
            output_stream=output,
            styled=False,
        )

        self.assertEqual(session.run(unexpected), 0)
        rendered = output.getvalue()
        self.assertIn("/help", rendered)
        self.assertIn(str(self.workspace.resolve()), rendered)
        self.assertIn("Session:", rendered)
        self.assertIn("(full:", rendered)
        self.assertIn("No tasks have run in this session.", rendered)
        self.assertIn("Screen clearing is unavailable", rendered)
        self.assertIn("Unknown command: /unknown", rendered)

    def test_eof_exits_without_running_a_task(self) -> None:
        output = io.StringIO()
        session = InteractiveSession(
            conversation=self.store.create(self.workspace),
            model_label="test-model",
            input_stream=io.StringIO(""),
            output_stream=output,
            styled=False,
        )

        self.assertEqual(session.run(lambda _: self._result(1)), 0)
        self.assertIn("Session ended.", output.getvalue())

    def test_keyboard_interrupt_at_prompt_keeps_session_open(self) -> None:
        output = io.StringIO()
        source = _InterruptOnce("/exit\n")
        session = InteractiveSession(
            conversation=self.store.create(self.workspace),
            model_label="test-model",
            input_stream=source,
            output_stream=output,
            styled=False,
        )

        self.assertEqual(session.run(lambda _: self._result(1)), 0)
        self.assertIn("Input cancelled", output.getvalue())

    def test_long_prior_request_is_bounded_in_next_objective(self) -> None:
        source = io.StringIO(("x" * 8_000) + "\nnext task\n/exit\n")
        objectives: list[str] = []
        session = InteractiveSession(
            conversation=self.store.create(self.workspace),
            model_label="test-model",
            input_stream=source,
            output_stream=io.StringIO(),
            styled=False,
        )

        def run_task(objective: str) -> RunResult:
            objectives.append(objective)
            return self._result(len(objectives))

        self.assertEqual(session.run(run_task), 0)
        self.assertEqual(len(objectives), 2)
        self.assertLess(len(objectives[1]), 19_000)
        self.assertIn("...[compacted]...", objectives[1])

    def test_new_process_can_resume_and_continue_with_assistant_response(self) -> None:
        conversation = self.store.create(self.workspace)
        first = InteractiveSession(
            conversation=conversation,
            model_label="test-model",
            input_stream=io.StringIO("Who are you?\n/exit\n"),
            output_stream=io.StringIO(),
            styled=False,
        )
        self.assertEqual(first.run(lambda _: self._result(1)), 0)

        objectives: list[str] = []
        output = io.StringIO()
        resumed = InteractiveSession(
            conversation=self.store.resume(conversation.session_id, self.workspace),
            model_label="test-model",
            input_stream=io.StringIO("What did you say?\n/exit\n"),
            output_stream=output,
            styled=False,
        )

        self.assertEqual(
            resumed.run(
                lambda objective: objectives.append(objective) or self._result(2)
            ),
            0,
        )
        self.assertIn("Who are you?", objectives[0])
        self.assertIn("summary-1", objectives[0])
        self.assertIn("resumed", output.getvalue().lower())

    def test_resume_command_lists_metadata_and_switches_without_running_agent(self) -> None:
        previous = self.store.create(self.workspace)
        previous.record("Fix parser tests", self._result(1))
        current = self.store.create(self.workspace)
        output = io.StringIO()
        session = InteractiveSession(
            conversation=current,
            model_label="test-model",
            input_stream=io.StringIO(
                f"/resume\n{previous.session_id[:8]}\n/history\n/exit\n"
            ),
            output_stream=output,
            styled=False,
        )

        def unexpected(_: str) -> RunResult:
            self.fail("resuming a session invoked the agent runner")

        self.assertEqual(session.run(unexpected), 0)
        self.assertEqual(session.conversation.session_id, previous.session_id)
        rendered = output.getvalue()
        self.assertIn("Resume session", rendered)
        self.assertIn("Fix parser tests", rendered)
        self.assertIn("Resumed session", rendered)

    @staticmethod
    def _result(index: int) -> RunResult:
        return RunResult(
            run_id=str(index) * 32,
            status=RunStatus.SUCCEEDED,
            summary=f"summary-{index}",
            changed_files=(f"file-{index}.txt",),
            verifications=(),
        )


class _InterruptOnce(io.StringIO):
    def __init__(self, value: str) -> None:
        super().__init__(value)
        self._interrupted = False

    def readline(self, *args: object, **kwargs: object) -> str:
        if not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt
        return super().readline(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
