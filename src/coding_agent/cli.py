"""Command-line composition root for the coding agent."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .context import SYSTEM_PROMPT
from .domain import RunOptions, RunStatus
from .events import Redactor
from .interactive import InteractiveSession
from .local_runner import LocalAgentRunner, LocalRunSettings
from .model import OpenAICompatibleAdapter
from .evaluation import EvaluationConfig, load_suite, run_evaluation
from .workspace import WorkspaceSetupError, WorkspaceSetupResult, prepare_workspace


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

    tui_parser = subparsers.add_parser("tui", help="open an interactive terminal session")
    _add_agent_arguments(tui_parser, include_json=False)

    run_parser = subparsers.add_parser("run", help="run one coding task")
    run_parser.add_argument("task", help="natural-language task objective")
    _add_agent_arguments(run_parser, include_json=True)

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
    eval_parser.add_argument(
        "--thinking",
        choices=("enabled", "disabled"),
        default=os.environ.get("CODING_AGENT_THINKING"),
        help="explicit provider thinking mode; omitted uses the provider default",
    )
    eval_parser.add_argument("--repetitions", type=int)
    eval_parser.add_argument("--max-turns", type=int, default=30)
    eval_parser.add_argument("--max-seconds", type=int, default=900)
    eval_parser.add_argument("--command-timeout", type=int, default=120)
    return parser


def _add_agent_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_json: bool,
) -> None:
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--model", default=os.environ.get("CODING_AGENT_MODEL"))
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CODING_AGENT_BASE_URL", "https://api.openai.com/v1"),
    )
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument(
        "--thinking",
        choices=("enabled", "disabled"),
        default=os.environ.get("CODING_AGENT_THINKING"),
        help="explicit provider thinking mode; omitted uses the provider default",
    )
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--command-timeout", type=int, default=120)
    parser.add_argument(
        "--approval-mode",
        choices=("prompt", "deny"),
        default="prompt",
        help="how to handle commands and deletions not pre-authorized at startup",
    )
    parser.add_argument(
        "--allow-program",
        action="append",
        default=[],
        metavar="NAME",
        help="pre-authorize one PATH-resolved executable name; may be repeated",
    )
    if include_json:
        parser.add_argument("--json", action="store_true", help="emit JSONL progress")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["tui"]
    args = parser.parse_args(arguments)
    try:
        if args.command == "tui":
            return _tui(args)
        if args.command == "run":
            return _run(args)
        if args.command == "inspect-run":
            return _inspect_run(args)
        if args.command == "eval":
            return _eval(args)
    except (ConfigurationError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 5
    return 5


def _run(args: argparse.Namespace) -> int:
    api_key = _validate_agent_configuration(args)
    setup = _prepare_agent_workspace(args.workspace)
    _render_workspace_setup(setup, json_output=args.json)
    runner = _build_local_runner(args, setup.workspace, api_key, json_output=args.json)
    result = runner.run(args.task)
    if not args.json:
        print(f"Run ID: {result.run_id}")
    return EXIT_CODES[result.status]


def _tui(args: argparse.Namespace) -> int:
    api_key = _validate_agent_configuration(args)
    setup = _prepare_agent_workspace(args.workspace)
    _render_workspace_setup(setup, json_output=False)
    runner = _build_local_runner(args, setup.workspace, api_key, json_output=False)
    session = InteractiveSession(
        workspace=setup.workspace,
        model_label=args.model,
    )
    return session.run(runner.run)


def _prepare_agent_workspace(path: Path) -> WorkspaceSetupResult:
    try:
        return prepare_workspace(path)
    except WorkspaceSetupError as exc:
        raise ConfigurationError(str(exc)) from exc


def _build_local_runner(
    args: argparse.Namespace,
    workspace: Path,
    api_key: str,
    *,
    json_output: bool,
) -> LocalAgentRunner:
    options = RunOptions(
        max_model_turns=args.max_turns,
        max_wall_seconds=args.max_seconds,
        default_command_timeout=args.command_timeout,
    )
    parsed_endpoint = urlparse(args.base_url)
    metadata = {
        "agent_version": __version__,
        "prompt_sha256": _prompt_hash(),
        "tool_schema_version": "1",
        "model_name": args.model,
        "thinking_mode": args.thinking or "provider_default",
        "endpoint_host": parsed_endpoint.hostname or "",
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "max_model_turns": args.max_turns,
        "max_wall_seconds": args.max_seconds,
    }
    return LocalAgentRunner(
        LocalRunSettings(
            workspace=workspace,
            api_key=api_key,
            model_name=args.model,
            base_url=args.base_url,
            thinking=args.thinking,
            options=options,
            approval_mode=args.approval_mode,
            allowed_programs=frozenset(args.allow_program),
            runs_root=_runs_root(),
            run_metadata=metadata,
            json_output=json_output,
        )
    )


def _validate_agent_configuration(args: argparse.Namespace) -> str:
    if not args.model:
        raise ConfigurationError("set --model or CODING_AGENT_MODEL")
    _validate_base_url(args.base_url)
    _validate_thinking(args.thinking)
    if args.max_turns <= 0 or args.max_turns > 200:
        raise ConfigurationError("--max-turns must be between 1 and 200")
    if args.max_seconds <= 0 or args.max_seconds > 7_200:
        raise ConfigurationError("--max-seconds must be between 1 and 7200")
    if args.command_timeout <= 0 or args.command_timeout > 600:
        raise ConfigurationError("--command-timeout must be between 1 and 600")

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise ConfigurationError(f"environment variable {args.api_key_env} is not set")
    return api_key


def _render_workspace_setup(
    result: WorkspaceSetupResult,
    *,
    json_output: bool,
) -> None:
    for message in result.messages:
        if json_output:
            print(
                json.dumps(
                    {"kind": "workspace_setup", "message": message},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(f"[SETUP] {message}")


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
    _validate_base_url(args.base_url)
    _validate_thinking(args.thinking)
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
        thinking=args.thinking,
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
                "thinking_mode": args.thinking or "provider_default",
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


def _validate_base_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("base URL must be an absolute HTTP(S) URL")


def _validate_thinking(value: str | None) -> None:
    if value not in {None, "enabled", "disabled"}:
        raise ConfigurationError("thinking mode must be enabled or disabled")


if __name__ == "__main__":
    raise SystemExit(main())
