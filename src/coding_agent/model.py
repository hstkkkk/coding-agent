"""Model seam and OpenAI-compatible chat-completions adapter."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from typing import Any, Iterable

from .domain import (
    AssistantTurn,
    BlockedRequest,
    FinishRequest,
    ModelAuthError,
    ModelPort,
    ModelProtocolError,
    ModelRequest,
    ModelRequestError,
    ModelTransientError,
    ToolCall,
    ToolDefinition,
    require_object,
    require_string,
    string_tuple,
)


class OpenAICompatibleAdapter(ModelPort):
    """A deliberately narrow adapter for /v1/chat/completions tool calls."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: int = 60,
        temperature: float = 0.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if not model:
            raise ValueError("model must not be empty")
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature

    def complete(self, request: ModelRequest) -> AssistantTurn:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "tools": [self._tool_payload(tool) for tool in request.tools],
            "tool_choice": "required",
            "temperature": self.temperature,
        }
        raw = self._post(payload)
        try:
            choices = raw["choices"]
            message = choices[0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelProtocolError("model response has no assistant message") from exc
        return self._normalize_message(message, request.tools)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        endpoint = self.base_url
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        encoded = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=encoded,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise ModelAuthError(f"model endpoint rejected credentials ({exc.code})") from exc
            if exc.code == 429 or exc.code >= 500:
                raise ModelTransientError(f"temporary model endpoint error ({exc.code})") from exc
            raise ModelRequestError(f"model endpoint rejected the request ({exc.code})") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise ModelTransientError(f"model request failed temporarily: {exc}") from exc

        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelProtocolError("model endpoint returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ModelProtocolError("model endpoint returned a non-object JSON value")
        return value

    @staticmethod
    def _tool_payload(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }

    @staticmethod
    def _normalize_message(
        message: Any,
        tools: tuple[ToolDefinition, ...],
    ) -> AssistantTurn:
        message_obj = require_object(message, "assistant message")
        tool_calls = message_obj.get("tool_calls")
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            count = len(tool_calls) if isinstance(tool_calls, list) else 0
            raise ModelProtocolError(
                f"assistant must return exactly one tool call; received {count}"
            )

        call = require_object(tool_calls[0], "tool call")
        call_id = require_string(call.get("id"), "tool call id")
        function = require_object(call.get("function"), "tool call function")
        name = require_string(function.get("name"), "tool name")
        allowed = {tool.name for tool in tools}
        if name not in allowed:
            raise ModelProtocolError(f"unknown tool: {name}")

        raw_arguments = function.get("arguments", "{}")
        if isinstance(raw_arguments, str):
            try:
                parsed = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise ModelProtocolError("tool arguments are not valid JSON") from exc
        else:
            parsed = raw_arguments
        arguments = dict(require_object(parsed, "tool arguments"))
        rationale_value = message_obj.get("content")
        rationale = rationale_value.strip() if isinstance(rationale_value, str) else ""

        if name == "finish":
            action = FinishRequest(
                call_id=call_id,
                summary=require_string(arguments.get("summary"), "finish summary"),
                verification_ids=string_tuple(
                    arguments.get("verification_ids"), "verification_ids"
                ),
                warnings=string_tuple(arguments.get("warnings"), "warnings"),
            )
        elif name == "report_blocked":
            action = BlockedRequest(
                call_id=call_id,
                reason=require_string(arguments.get("reason"), "blocked reason"),
                needed=str(arguments.get("needed", "")).strip(),
            )
        else:
            action = ToolCall(call_id=call_id, name=name, arguments=arguments)
        return AssistantTurn(rationale=rationale, action=action)


ScriptedResponse = AssistantTurn | Exception | Callable[[ModelRequest], AssistantTurn]


class ScriptedModelAdapter(ModelPort):
    """Deterministic model adapter for AgentEngine scenario tests."""

    def __init__(self, responses: Iterable[ScriptedResponse]) -> None:
        self._responses = deque(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> AssistantTurn:
        self.requests.append(request)
        if not self._responses:
            raise ModelProtocolError("scripted model response queue is empty")
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(request)
        return response
