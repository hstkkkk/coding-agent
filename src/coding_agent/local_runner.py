"""Reusable local composition for one bounded agent run."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .artifacts import ArtifactStore
from .domain import ModelPort, RunOptions, RunResult, TaskRequest
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


@dataclass(frozen=True, slots=True)
class LocalRunSettings:
    workspace: Path
    api_key: str = field(repr=False)
    model_name: str
    base_url: str
    thinking: str | None
    options: RunOptions
    approval_mode: str
    allowed_programs: frozenset[str]
    runs_root: Path
    run_metadata: Mapping[str, object]
    json_output: bool = False


class LocalAgentRunner:
    """Create isolated run state while reusing validated session settings."""

    def __init__(
        self,
        settings: LocalRunSettings,
        *,
        model: ModelPort | None = None,
    ) -> None:
        self.settings = settings
        self.redactor = Redactor([settings.api_key])
        self.model = model or OpenAICompatibleAdapter(
            api_key=settings.api_key,
            model=settings.model_name,
            base_url=settings.base_url,
            thinking=settings.thinking,
        )
        fallback = (
            PromptApprovalAdapter()
            if settings.approval_mode == "prompt"
            else DenyApprovalAdapter()
        )
        self.approvals = ScopedApprovalAdapter(fallback, set(settings.allowed_programs))

    def run(self, objective: str) -> RunResult:
        run_id = uuid.uuid4().hex
        run_dir = self.settings.runs_root / run_id
        log_sink = JsonlEventSink(run_dir / "events.jsonl", self.redactor)
        display_sink = (
            JsonConsoleEventSink(self.redactor)
            if self.settings.json_output
            else ConsoleEventSink(self.redactor)
        )
        events = CompositeEventSink(log_sink, display_sink)
        artifacts = ArtifactStore(run_dir / "artifacts", self.redactor)
        tools = LocalToolRuntime(
            workspace=self.settings.workspace,
            approvals=self.approvals,
            artifacts=artifacts,
            redactor=self.redactor,
            default_command_timeout=self.settings.options.default_command_timeout,
        )
        engine = AgentEngine(
            model=self.model,
            tools=tools,
            events=events,
            options=self.settings.options,
            run_metadata=dict(self.settings.run_metadata),
        )
        return engine.run(
            TaskRequest(
                objective=objective,
                workspace=self.settings.workspace,
                run_id=run_id,
            )
        )
