"""Repeatable local evaluation harness with an independent oracle."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .domain import ModelPort, RunOptions, RunStatus, TaskRequest
from .engine import AgentEngine
from .events import JsonlEventSink, Redactor
from .policy import DenyApprovalAdapter, ScopedApprovalAdapter
from .tools import LocalToolRuntime
from .tools.process import ProcessExecution, ProcessRunner


@dataclass(frozen=True, slots=True)
class OracleSpec:
    program: str
    args: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: int = 120


@dataclass(frozen=True, slots=True)
class HiddenFile:
    source: Path
    destination: str


@dataclass(frozen=True, slots=True)
class EvaluationTask:
    name: str
    source: Path
    objective: str
    oracle: OracleSpec
    hidden_files: tuple[HiddenFile, ...] = ()


@dataclass(frozen=True, slots=True)
class EvaluationSuite:
    tasks: tuple[EvaluationTask, ...]
    repetitions: int = 1
    allowed_programs: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    model: ModelPort
    options: RunOptions
    runs_root: Path
    redactor: Redactor
    metadata: dict[str, Any]


def load_suite(path: Path) -> EvaluationSuite:
    suite_path = path.resolve(strict=True)
    try:
        raw = json.loads(suite_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"evaluation suite is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
        raise ValueError("evaluation suite must contain a tasks array")
    base = suite_path.parent
    tasks: list[EvaluationTask] = []
    for index, item in enumerate(raw["tasks"]):
        if not isinstance(item, dict):
            raise ValueError(f"task {index} must be an object")
        name = _required_string(item, "name", index)
        objective = _required_string(item, "objective", index)
        source = _suite_relative(base, _required_string(item, "source", index))
        if not source.is_dir():
            raise ValueError(f"task {name} source is not a directory")
        oracle_raw = item.get("oracle")
        if not isinstance(oracle_raw, dict):
            raise ValueError(f"task {name} must define an oracle object")
        program = _required_string(oracle_raw, "program", index)
        args = oracle_raw.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError(f"task {name} oracle args must be strings")
        timeout = oracle_raw.get("timeout_seconds", 120)
        if not isinstance(timeout, int) or not 1 <= timeout <= 600:
            raise ValueError(f"task {name} oracle timeout must be between 1 and 600")
        hidden: list[HiddenFile] = []
        for hidden_raw in item.get("hidden_files", []):
            if not isinstance(hidden_raw, dict):
                raise ValueError(f"task {name} hidden file entry must be an object")
            hidden.append(
                HiddenFile(
                    source=_suite_relative(
                        base,
                        _required_string(hidden_raw, "source", index),
                    ),
                    destination=_safe_relative(
                        _required_string(hidden_raw, "destination", index)
                    ),
                )
            )
        tasks.append(
            EvaluationTask(
                name=name,
                source=source,
                objective=objective,
                oracle=OracleSpec(
                    program=program,
                    args=tuple(args),
                    cwd=_safe_relative(str(oracle_raw.get("cwd", "."))),
                    timeout_seconds=timeout,
                ),
                hidden_files=tuple(hidden),
            )
        )
    repetitions = raw.get("repetitions", 1)
    if not isinstance(repetitions, int) or not 1 <= repetitions <= 5:
        raise ValueError("repetitions must be between 1 and 5")
    allowed = raw.get("allowed_programs", [])
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise ValueError("allowed_programs must be an array of strings")
    if not tasks:
        raise ValueError("evaluation suite must contain at least one task")
    return EvaluationSuite(tuple(tasks), repetitions, frozenset(allowed))


def run_evaluation(
    suite: EvaluationSuite,
    config: EvaluationConfig,
    *,
    repetitions: int | None = None,
) -> tuple[Path, dict[str, Any]]:
    repeat_count = repetitions if repetitions is not None else suite.repetitions
    if not 1 <= repeat_count <= 5:
        raise ValueError("repetitions must be between 1 and 5")
    evaluation_id = uuid.uuid4().hex
    report_dir = config.runs_root / "evaluations" / evaluation_id
    report_dir.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []

    for task in suite.tasks:
        for repetition in range(1, repeat_count + 1):
            entry = _run_task(task, repetition, suite.allowed_programs, config)
            entries.append(entry)
            print(
                f"[EVAL] {task.name} run {repetition}: "
                f"agent={entry['agent_status']} oracle={entry['oracle_exit_code']} "
                f"passed={entry['passed']}"
            )

    passed = sum(1 for entry in entries if entry["passed"])
    false_successes = sum(1 for entry in entries if entry["false_success"])
    report: dict[str, Any] = {
        "evaluation_id": evaluation_id,
        "runs": len(entries),
        "passed": passed,
        "pass_rate": passed / len(entries),
        "false_successes": false_successes,
        "entries": entries,
    }
    sanitized = config.redactor.value(report)
    assert isinstance(sanitized, dict)
    report_path = report_dir / "report.json"
    report_path.write_text(
        json.dumps(sanitized, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    return report_path, sanitized


def _run_task(
    task: EvaluationTask,
    repetition: int,
    allowed_programs: frozenset[str],
    config: EvaluationConfig,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="coding-agent-eval-") as directory:
        workspace = Path(directory) / "workspace"
        shutil.copytree(
            task.source,
            workspace,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        _initialize_git(workspace)
        process_runner = ProcessRunner()
        baseline = _run_oracle(process_runner, task.oracle, workspace)

        run_id = uuid.uuid4().hex
        run_dir = config.runs_root / run_id
        artifacts = ArtifactStore(run_dir / "artifacts", config.redactor)
        events = JsonlEventSink(run_dir / "events.jsonl", config.redactor)
        approvals = ScopedApprovalAdapter(DenyApprovalAdapter(), set(allowed_programs))
        runtime = LocalToolRuntime(
            workspace=workspace,
            approvals=approvals,
            artifacts=artifacts,
            redactor=config.redactor,
            default_command_timeout=config.options.default_command_timeout,
        )
        engine = AgentEngine(
            model=config.model,
            tools=runtime,
            events=events,
            options=config.options,
            run_metadata=config.metadata,
        )
        result = engine.run(TaskRequest(task.objective, workspace, run_id=run_id))

        for hidden in task.hidden_files:
            destination = _workspace_destination(workspace, hidden.destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(hidden.source, destination)
        oracle = _run_oracle(process_runner, task.oracle, workspace)
        passed = result.status is RunStatus.SUCCEEDED and oracle.exit_code == 0
        false_success = result.status is RunStatus.SUCCEEDED and oracle.exit_code != 0
        return {
            "task": task.name,
            "repetition": repetition,
            "run_id": run_id,
            "agent_status": result.status.value,
            "agent_error_code": result.error_code.value if result.error_code else None,
            "baseline_exit_code": baseline.exit_code,
            "oracle_exit_code": oracle.exit_code,
            "oracle_timed_out": oracle.timed_out,
            "passed": passed,
            "false_success": false_success,
            "model_turns": result.model_turns,
            "tool_calls": result.tool_calls,
            "workspace_version": result.workspace_version,
            "changed_files": list(result.changed_files),
            "oracle_output": _preview(oracle.stdout + "\n" + oracle.stderr, 4_000),
        }


def _run_oracle(runner: ProcessRunner, oracle: OracleSpec, workspace: Path) -> ProcessExecution:
    cwd = _workspace_destination(workspace, oracle.cwd)
    if not cwd.is_dir():
        raise ValueError("oracle cwd is not a directory")
    return runner.run(
        program=oracle.program,
        args=list(oracle.args),
        cwd=cwd,
        timeout_seconds=oracle.timeout_seconds,
    )


def _initialize_git(workspace: Path) -> None:
    commands = (
        ("init", "-q"),
        ("config", "user.name", "Candidate"),
        ("config", "user.email", "candidate@example.invalid"),
        ("add", "."),
        ("commit", "-q", "-m", "evaluation fixture"),
    )
    for args in commands:
        result = subprocess.run(
            ["git", *args],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            raise ValueError(f"could not initialize evaluation Git repository: {result.stderr}")


def _suite_relative(base: Path, raw: str) -> Path:
    relative = Path(raw)
    if relative.is_absolute() or relative.drive:
        raise ValueError("suite paths must be relative")
    resolved = (base / relative).resolve(strict=True)
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError("suite path escapes the suite directory") from exc
    return resolved


def _safe_relative(raw: str) -> str:
    path = Path(raw)
    if path.is_absolute() or path.drive or ".." in path.parts:
        raise ValueError("path must be relative and must not contain ..")
    return path.as_posix()


def _workspace_destination(workspace: Path, raw: str) -> Path:
    relative = _safe_relative(raw)
    resolved = (workspace / relative).resolve(strict=False)
    try:
        resolved.relative_to(workspace.resolve())
    except ValueError as exc:
        raise ValueError("path escapes evaluation workspace") from exc
    return resolved


def _required_string(value: dict[str, Any], key: str, task_index: int) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"task {task_index} field {key} must be a non-empty string")
    return result.strip()


def _preview(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n...[oracle output truncated]...\n" + text[-half:]

