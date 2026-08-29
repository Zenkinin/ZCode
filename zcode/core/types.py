from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AgentState(StrEnum):
    READY = "ready"
    THINKING = "thinking"
    PLANNING = "planning"
    EXECUTING = "executing"
    RUNNING = "running"
    VERIFYING = "verifying"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = "{}"
    parse_error: str | None = None


@dataclass(slots=True)
class Message:
    role: Role
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning_content: str | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(slots=True)
class ModelResponse:
    text: str | None
    tool_calls: list[ToolCall]
    finish_reason: str | None
    assistant_message: Message
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(slots=True)
class ToolResult:
    success: bool
    content: str
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
