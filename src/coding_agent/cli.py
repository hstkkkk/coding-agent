"""Command-line composition root for the coding agent."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .artifacts import ArtifactStore
from .conversation import (
    ConversationError,
    ConversationLimits,
    ConversationStore,
)
from .context import SYSTEM_PROMPT
from .domain import RunOptions, RunStatus
from .evaluation import EvaluationConfig, load_suite, run_evaluation
from .events import Redactor
from .interactive import InteractiveSession
from .local_runner import LocalAgentRunner, LocalRunSettings
from .model import OpenAICompatibleAdapter
from .settings import (
    SettingsError,
    UserSettings,
    load_user_settings,
)
from .workspace import WorkspaceSetupError, WorkspaceSetupResult, prepare_workspace


EXIT_CODES = {
    RunStatus.SUCCEEDED: 0,
    RunStatus.ANSWERED: 0,
    RunStatus.BLOCKED: 2,
    RunStatus.FAILED: 3,
    RunStatus.CANCELLED: 4,
}


class ConfigurationError(ValueError):
    pass


def build_parser(settings: UserSettings | None = None) -> argparse.ArgumentParser:
    settings = settings or UserSettings()
    parser = argparse.ArgumentParser(
        prog="coding-agent",
        description="Run a bounded coding agent in one local Git workspace.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    tui_parser = subparsers.add_parser("tui", help="open an interactive terminal session")
    _add_agent_arguments(tui_parser, settings, include_json=False)
    _add_session_arguments(tui_parser, settings)
    tui_parser.set_defaults(session_id=None)

    resume_parser = subparsers.add_parser(
        "resume",
        help="resume a persisted interactive session",
    )
    resume_parser.add_argument(
        "session_id",
        metavar="REFERENCE",
        help="full session ID or unique hexadecimal prefix (at least 2 characters)",
    )
    _add_agent_arguments(resume_parser, settings, include_json=False)
    _add_session_arguments(resume_parser, settings)

    sessions_parser = subparsers.add_parser(
        "sessions",
        help="list persisted interactive sessions",
    )
    sessions_parser.add_argument("--workspace", type=Path)
    sessions_parser.add_argument("--limit", type=int, default=20)

    run_parser = subparsers.add_parser("run", help="run one coding task")
    run_parser.add_argument("task", help="natural-language task objective")
    _add_agent_arguments(run_parser, settings, include_json=True)

    inspect_parser = subparsers.add_parser("inspect-run", help="show a saved run log")
    inspect_parser.add_argument("run_id")
    inspect_parser.add_argument("--json", action="store_true")

    recover_parser = subparsers.add_parser(
        "recover-file",
        help="copy a saved before-image to a new file",
    )
    recover_parser.add_argument("run_id")
    recover_parser.add_argument("recovery_id")
    recover_parser.add_argument("--output", type=Path, required=True)

    screenshot_parser = subparsers.add_parser(
        "export-screenshot",
        help="copy a saved browser screenshot to a new PNG file",
    )
    screenshot_parser.add_argument("run_id")
    screenshot_parser.add_argument("screenshot_id")
    screenshot_parser.add_argument("--output", type=Path, required=True)

    eval_parser = subparsers.add_parser("eval", help="run a repeatable local evaluation suite")
    eval_parser.add_argument("--suite", type=Path, required=True)
    _add_execution_arguments(eval_parser, settings)
    eval_parser.add_argument("--repetitions", type=int)
    parser.set_defaults(
        user_settings=settings,
        configured_runs_dir=settings.runs_dir,
        configured_sessions_dir=settings.sessions_dir,
        configured_allow_programs=settings.allow_programs,
    )
    return parser


def _add_agent_arguments(
    parser: argparse.ArgumentParser,
    settings: UserSettings,
    *,
    include_json: bool,
) -> None:
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    _add_execution_arguments(parser, settings)
    parser.add_argument(
        "--approval-mode",
        choices=("prompt", "deny"),
        default=_configured_value(settings, "approval_mode", default="prompt"),
        help="how to handle execution and destructive writes not pre-authorized at startup",
    )
    parser.add_argument(
        "--allow-program",
        action="append",
        default=None,
        metavar="NAME",
        help="pre-authorize one PATH-resolved executable name; may be repeated",
    )
    if include_json:
        parser.add_argument("--json", action="store_true", help="emit JSONL progress")


def _add_execution_arguments(
    parser: argparse.ArgumentParser,
    settings: UserSettings,
) -> None:
    parser.add_argument(
        "--model",
        default=_configured_value(
            settings,
            "model",
            environment_name="CODING_AGENT_MODEL",
        ),
    )
    parser.add_argument(
        "--base-url",
        default=_configured_value(
            settings,
            "base_url",
            environment_name="CODING_AGENT_BASE_URL",
            default="https://api.openai.com/v1",
        ),
    )
    parser.add_argument(
        "--api-key-env",
        default=_configured_value(settings, "api_key_env", default="OPENAI_API_KEY"),
    )
    parser.add_argument(
        "--thinking",
        choices=("enabled", "disabled"),
        default=_configured_value(
            settings,
            "thinking",
            environment_name="CODING_AGENT_THINKING",
        ),
        help="explicit provider thinking mode; omitted uses the provider default",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=_configured_value(settings, "max_turns", default=30),
    )
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=_configured_value(settings, "max_seconds", default=900),
    )
    parser.add_argument(
        "--command-timeout",
        type=int,
        default=_configured_value(settings, "command_timeout", default=120),
    )


def _add_session_arguments(
    parser: argparse.ArgumentParser,
    settings: UserSettings,
) -> None:
    parser.add_argument(
        "--session-context-chars",
        type=int,
        default=_configured_value(
            settings,
            "session_context_chars",
            default=12_000,
        ),
        metavar="N",
        help="maximum persisted-history characters sent with each request",
    )


def _configured_value(
    settings: UserSettings,
    field_name: str,
    *,
    environment_name: str | None = None,
    default: object = None,
) -> object:
    value = getattr(settings, field_name)
    if settings.provides(field_name) or value is not None:
        return value
    if environment_name is not None:
        return os.environ.get(environment_name, default)
    return default


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["tui"]
    try:
        settings = UserSettings() if _requests_help(arguments) else load_user_settings()
    except SettingsError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 5
    parser = build_parser(settings)
    args = parser.parse_args(arguments)
    try:
        if args.command == "tui":
            return _tui(args)
        if args.command == "resume":
            return _tui(args)
        if args.command == "sessions":
            return _sessions(args)
        if args.command == "run":
            return _run(args)
        if args.command == "inspect-run":
            return _inspect_run(args)
        if args.command == "recover-file":
            return _recover_file(args)
        if args.command == "export-screenshot":
            return _export_screenshot(args)
        if args.command == "eval":
            return _eval(args)
    except (ConfigurationError, ConversationError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 5
    return 5


def _requests_help(arguments: list[str]) -> bool:
    return any(argument in {"-h", "--help"} for argument in arguments)


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
    _validate_session_context_chars(args.session_context_chars)
    store = ConversationStore(
        _sessions_root(args.configured_sessions_dir),
        Redactor([api_key]),
        limits=_conversation_limits(args.session_context_chars),
    )
    if args.session_id is not None:
        conversation = store.resume(args.session_id, args.workspace)
        setup = _prepare_agent_workspace(conversation.workspace)
    else:
        setup = _prepare_agent_workspace(args.workspace)
        conversation = store.create(setup.workspace)
    _render_workspace_setup(setup, json_output=False)
    runner = _build_local_runner(args, setup.workspace, api_key, json_output=False)
    session = InteractiveSession(
        conversation=conversation,
        model_label=args.model,
    )
    return session.run(runner.run)


def _sessions(args: argparse.Namespace) -> int:
    if not 1 <= args.limit <= 100:
        raise ConfigurationError("--limit must be between 1 and 100")
    store = ConversationStore(
        _sessions_root(args.configured_sessions_dir),
        Redactor(),
    )
    sessions = store.list_sessions(workspace=args.workspace, limit=args.limit)
    if not sessions:
        print("No persisted sessions found.")
        return 0
    for session in sessions:
        timestamp = session.last_turn_at or session.created_at
        last_request = (
            _one_line(session.last_request)
            if session.last_request
            else "no completed request"
        )
        print(
            f"{session.reference}  {_local_time(timestamp)}  "
            f"turns={session.turn_count} compacted={session.compacted_turns}  "
            f"last={last_request}  {session.workspace}"
        )
    return 0


def _local_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().isoformat(timespec="minutes")
    except ValueError:
        return "unknown-time"


def _one_line(value: str, *, limit: int = 80) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in value
    )
    normalized = " ".join(printable.split()) or "[empty]"
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


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
        "tool_schema_version": "3",
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
            allowed_programs=frozenset(
                args.allow_program
                if args.allow_program is not None
                else args.configured_allow_programs
            ),
            runs_root=_runs_root(args.configured_runs_dir),
            run_metadata=metadata,
            json_output=json_output,
        )
    )


def _validate_agent_configuration(args: argparse.Namespace) -> str:
    if not args.model:
        raise ConfigurationError(
            "set --model, 'model' in ~/.coding-agent/settings.json, "
            "or CODING_AGENT_MODEL"
        )
    _validate_base_url(args.base_url)
    _validate_thinking(args.thinking)
    if args.max_turns <= 0 or args.max_turns > 200:
        raise ConfigurationError("--max-turns must be between 1 and 200")
    if args.max_seconds <= 0 or args.max_seconds > 7_200:
        raise ConfigurationError("--max-seconds must be between 1 and 7200")
    if args.command_timeout <= 0 or args.command_timeout > 600:
        raise ConfigurationError("--command-timeout must be between 1 and 600")

    api_key = args.user_settings.api_key or os.environ.get(args.api_key_env)
    if not api_key:
        raise ConfigurationError(
            "set 'api_key' in ~/.coding-agent/settings.json or environment variable "
            f"{args.api_key_env}"
        )
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
    _validate_hex_id(args.run_id, "run_id")
    path = _runs_root(args.configured_runs_dir) / args.run_id / "events.jsonl"
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
            recovery_id = data.get("recovery_output_id")
            if isinstance(recovery_id, str) and recovery_id:
                print(
                    "    RECOVERY "
                    f"{data.get('recovery_path', 'file')} {recovery_id}"
                )
            screenshot_id = data.get("screenshot_id")
            if isinstance(screenshot_id, str) and screenshot_id:
                print(f"    SCREENSHOT {screenshot_id}")
        elif kind == "terminal":
            label = "ANSWER" if data.get("status") == "ANSWERED" else "DONE"
            print(
                f"{event['sequence']:>3} {label:<6} "
                f"{data.get('status', '')}: {data.get('summary', '')}"
            )
    return 0


def _recover_file(args: argparse.Namespace) -> int:
    _validate_hex_id(args.run_id, "run_id")
    _validate_hex_id(args.recovery_id, "recovery_id")
    artifacts_root = _runs_root(args.configured_runs_dir) / args.run_id / "artifacts"
    if not artifacts_root.is_dir():
        raise ConfigurationError("run artifacts were not found")
    try:
        store = ArtifactStore(artifacts_root, Redactor())
        content, total = store.read_text(args.recovery_id, 0, 1_000_001)
    except FileNotFoundError as exc:
        raise ConfigurationError("recovery copy was not found") from exc
    if len(content) != total:
        raise ConfigurationError("recovery copy exceeds the supported size")

    output = args.output.expanduser().resolve()
    if not output.parent.is_dir():
        raise ConfigurationError("recovery output directory does not exist")
    try:
        with output.open("x", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ConfigurationError("recovery output already exists") from exc
    print(f"Recovered before-image to {output}")
    return 0


def _export_screenshot(args: argparse.Namespace) -> int:
    _validate_hex_id(args.run_id, "run_id")
    _validate_hex_id(args.screenshot_id, "screenshot_id")
    source = (
        _runs_root(args.configured_runs_dir)
        / args.run_id
        / "browser"
        / f"{args.screenshot_id}.png"
    )
    try:
        size = source.stat().st_size
        if size <= 8 or size > 20 * 1024 * 1024:
            raise ConfigurationError("browser screenshot has an invalid size")
        content = source.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigurationError("browser screenshot was not found") from exc
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ConfigurationError("browser screenshot is not a PNG file")

    output = args.output.expanduser().resolve()
    if not output.parent.is_dir():
        raise ConfigurationError("screenshot output directory does not exist")
    try:
        with output.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ConfigurationError("screenshot output already exists") from exc
    print(f"Exported browser screenshot to {output}")
    return 0


def _validate_hex_id(value: str, label: str) -> None:
    if len(value) != 32 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ConfigurationError(
            f"{label} must be 32 lowercase hexadecimal characters"
        )


def _eval(args: argparse.Namespace) -> int:
    api_key = _validate_agent_configuration(args)
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
            runs_root=_runs_root(args.configured_runs_dir),
            redactor=redactor,
            metadata={
                "agent_version": __version__,
                "prompt_sha256": _prompt_hash(),
                "tool_schema_version": "3",
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


def _runs_root(configured_path: Path | None = None) -> Path:
    if configured_path is not None:
        return configured_path.expanduser().resolve()
    configured = os.environ.get("CODING_AGENT_RUNS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".coding-agent" / "runs").resolve()


def _sessions_root(configured_path: Path | None = None) -> Path:
    if configured_path is not None:
        return configured_path.expanduser().resolve()
    configured = os.environ.get("CODING_AGENT_SESSIONS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".coding-agent" / "sessions").resolve()


def _conversation_limits(max_context_chars: int) -> ConversationLimits:
    return ConversationLimits(
        max_context_chars=max_context_chars,
        target_context_chars=max(1_000, max_context_chars * 2 // 3),
    )


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


def _validate_session_context_chars(value: int) -> None:
    if not 2_000 <= value <= 18_000:
        raise ConfigurationError(
            "--session-context-chars must be between 2000 and 18000"
        )


if __name__ == "__main__":
    raise SystemExit(main())
