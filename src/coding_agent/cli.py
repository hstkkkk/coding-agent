"""Command-line composition root for the coding agent."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .artifacts import ArtifactStore
from .context import SYSTEM_PROMPT
from .domain import RunOptions, RunStatus, TaskRequest
from .engine import AgentEngine
from .events import (
    CompositeEventSink,
    ConsoleEventSink,
    JsonConsoleEventSink,
    JsonlEventSink,
    Redactor,
)
from .model import OpenAICompatibleAdapter
from .policy import DenyApprovalAdapter, PromptApprovalAdapter, ScopedApprovalAdapter
from .tools import LocalToolRuntime
from .evaluation import EvaluationConfig, load_suite, run_evaluation


EXIT_CODES = {
    RunStatus.SUCCEEDED: 0,
    RunStatus.BLOCKED: 2,
    RunStatus.FAILED: 3,
    RunStatus.CANCELLED: 4,
}


class ConfigurationError(ValueError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-agent",
        description="Run a bounded coding agent in one local Git workspace.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run one coding task")
    run_parser.add_argument("task", help="natural-language task objective")
    run_parser.add_argument("--workspace", type=Path, default=Path.cwd())
    run_parser.add_argument("--model", default=os.environ.get("CODING_AGENT_MODEL"))
    run_parser.add_argument(
        "--base-url",
        default=os.environ.get("CODING_AGENT_BASE_URL", "https://api.openai.com/v1"),
    )
    run_parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    run_parser.add_argument("--max-turns", type=int, default=30)
    run_parser.add_argument("--max-seconds", type=int, default=900)
    run_parser.add_argument("--command-timeout", type=int, default=120)
    run_parser.add_argument(
        "--approval-mode",
        choices=("prompt", "deny"),
        default="prompt",
        help="how to handle commands and deletions not pre-authorized at startup",
    )
    run_parser.add_argument(
        "--allow-program",
        action="append",
        default=[],
        metavar="NAME",
        help="pre-authorize one PATH-resolved executable name; may be repeated",
    )
    run_parser.add_argument("--json", action="store_true", help="emit JSONL progress")

    inspect_parser = subparsers.add_parser("inspect-run", help="show a saved run log")
    inspect_parser.add_argument("run_id")
    inspect_parser.add_argument("--json", action="store_true")

    eval_parser = subparsers.add_parser("eval", help="run a repeatable local evaluation suite")
    eval_parser.add_argument("--suite", type=Path, required=True)
    eval_parser.add_argument("--model", default=os.environ.get("CODING_AGENT_MODEL"))
    eval_parser.add_argument(
        "--base-url",
        default=os.environ.get("CODING_AGENT_BASE_URL", "https://api.openai.com/v1"),
    )
    eval_parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    eval_parser.add_argument("--repetitions", type=int)
    eval_parser.add_argument("--max-turns", type=int, default=30)
    eval_parser.add_argument("--max-seconds", type=int, default=900)
    eval_parser.add_argument("--command-timeout", type=int, default=120)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "inspect-run":
            return _inspect_run(args)
        if args.command == "eval":
            return _eval(args)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 5
    return 5


def _run(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise ConfigurationError("workspace does not exist or is not a directory")
    if not (workspace / ".git").exists():
        raise ConfigurationError("workspace must be a Git repository")
    if not args.model:
        raise ConfigurationError("set --model or CODING_AGENT_MODEL")
    if args.max_turns <= 0 or args.max_turns > 200:
        raise ConfigurationError("--max-turns must be between 1 and 200")
    if args.max_seconds <= 0 or args.max_seconds > 7_200:
        raise ConfigurationError("--max-seconds must be between 1 and 7200")
    if args.command_timeout <= 0 or args.command_timeout > 600:
        raise ConfigurationError("--command-timeout must be between 1 and 600")

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise ConfigurationError(f"environment variable {args.api_key_env} is not set")

    run_id = uuid.uuid4().hex
    run_dir = _runs_root() / run_id
    redactor = Redactor([api_key])
    event_file = run_dir / "events.jsonl"
    log_sink = JsonlEventSink(event_file, redactor)
    display_sink = JsonConsoleEventSink(redactor) if args.json else ConsoleEventSink(redactor)
    events = CompositeEventSink(log_sink, display_sink)
    artifacts = ArtifactStore(run_dir / "artifacts", redactor)

    fallback = PromptApprovalAdapter() if args.approval_mode == "prompt" else DenyApprovalAdapter()
    approvals = ScopedApprovalAdapter(fallback, set(args.allow_program))
    options = RunOptions(
        max_model_turns=args.max_turns,
        max_wall_seconds=args.max_seconds,
        default_command_timeout=args.command_timeout,
    )
    model = OpenAICompatibleAdapter(
        api_key=api_key,
        model=args.model,
        base_url=args.base_url,
    )
    tools = LocalToolRuntime(
        workspace=workspace,
        approvals=approvals,
        artifacts=artifacts,
        redactor=redactor,
        default_command_timeout=args.command_timeout,
    )
    parsed_endpoint = urlparse(args.base_url)
    engine = AgentEngine(
        model=model,
        tools=tools,
        events=events,
        options=options,
        run_metadata={
            "agent_version": __version__,
            "prompt_sha256": _prompt_hash(),
            "tool_schema_version": "1",
            "model_name": args.model,
            "endpoint_host": parsed_endpoint.hostname or "",
            "platform": platform.system(),
            "python_version": platform.python_version(),
            "max_model_turns": args.max_turns,
            "max_wall_seconds": args.max_seconds,
        },
    )
    result = engine.run(TaskRequest(args.task, workspace, run_id=run_id))
    if not args.json:
        print(f"Run ID: {result.run_id}")
    return EXIT_CODES[result.status]


def _inspect_run(args: argparse.Namespace) -> int:
    if len(args.run_id) != 32 or any(
        character not in "0123456789abcdef" for character in args.run_id
    ):
        raise ConfigurationError("run_id must be 32 lowercase hexadecimal characters")
    path = _runs_root() / args.run_id / "events.jsonl"
    if not path.is_file():
        raise ConfigurationError("run log was not found")
    lines = path.read_text(encoding="utf-8").splitlines()
    if args.json:
        for line in lines:
            print(line)
        return 0
    for line in lines:
        event = json.loads(line)
        kind = event.get("kind", "unknown")
        data = event.get("data", {})
        if kind == "model_action":
            print(f"{event['sequence']:>3} MODEL {data.get('action', '')}")
        elif kind == "tool_finished":
            print(
                f"{event['sequence']:>3} TOOL  {data.get('tool', '')} "
                f"{data.get('status', '')}"
            )
        elif kind == "terminal":
            print(f"{event['sequence']:>3} DONE  {data.get('status', '')}: {data.get('summary', '')}")
    return 0


def _eval(args: argparse.Namespace) -> int:
    if not args.model:
        raise ConfigurationError("set --model or CODING_AGENT_MODEL")
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise ConfigurationError(f"environment variable {args.api_key_env} is not set")
    if not 1 <= args.max_turns <= 200:
        raise ConfigurationError("--max-turns must be between 1 and 200")
    if not 1 <= args.max_seconds <= 7_200:
        raise ConfigurationError("--max-seconds must be between 1 and 7200")
    if not 1 <= args.command_timeout <= 600:
        raise ConfigurationError("--command-timeout must be between 1 and 600")
    if args.repetitions is not None and not 1 <= args.repetitions <= 5:
        raise ConfigurationError("--repetitions must be between 1 and 5")
    try:
        suite = load_suite(args.suite)
    except (OSError, ValueError) as exc:
        raise ConfigurationError(str(exc)) from exc

    redactor = Redactor([api_key])
    options = RunOptions(
        max_model_turns=args.max_turns,
        max_wall_seconds=args.max_seconds,
        default_command_timeout=args.command_timeout,
    )
    model = OpenAICompatibleAdapter(
        api_key=api_key,
        model=args.model,
        base_url=args.base_url,
    )
    endpoint = urlparse(args.base_url)
    _, report = run_evaluation(
        suite,
        EvaluationConfig(
            model=model,
            options=options,
            runs_root=_runs_root(),
            redactor=redactor,
            metadata={
                "agent_version": __version__,
                "prompt_sha256": _prompt_hash(),
                "tool_schema_version": "1",
                "model_name": args.model,
                "endpoint_host": endpoint.hostname or "",
                "platform": platform.system(),
                "python_version": platform.python_version(),
            },
        ),
        repetitions=args.repetitions,
    )
    print(
        f"Evaluation {report['evaluation_id']}: "
        f"{report['passed']}/{report['runs']} passed, "
        f"false successes={report['false_successes']}"
    )
    return 0 if report["passed"] == report["runs"] else 3


def _runs_root() -> Path:
    configured = os.environ.get("CODING_AGENT_RUNS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".coding-agent" / "runs").resolve()


def _prompt_hash() -> str:
    import hashlib

    return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
