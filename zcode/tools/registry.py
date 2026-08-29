from __future__ import annotations

from collections.abc import Iterable

from zcode.core.types import ToolCall, ToolDefinition, ToolResult
from zcode.tools.base import Tool


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = tool

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def activity_for(self, name: str) -> str:
        tool = self._tools.get(name)
        return tool.activity if tool else "executing"

    async def execute(self, call: ToolCall) -> ToolResult:
        if call.parse_error:
            return ToolResult(
                success=False,
                content=f"Invalid JSON arguments: {call.parse_error}",
                error_code="invalid_arguments",
            )
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                success=False,
                content=f"Unknown tool: {call.name}",
                error_code="unknown_tool",
            )
        try:
            return await tool.execute(call.arguments)
        except (TypeError, ValueError) as exc:
            return ToolResult(
                success=False,
                content=str(exc),
                error_code="invalid_arguments",
            )
        except Exception as exc:  # The tool boundary must not crash the agent loop.
            return ToolResult(
                success=False,
                content=f"{type(exc).__name__}: {exc}",
                error_code="tool_error",
            )
