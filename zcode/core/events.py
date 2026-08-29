from __future__ import annotations

from zcode.core.plan import PlanManager
from zcode.core.types import AgentState, ToolCall, ToolResult


class EventSink:
    """No-op observer used by the core so UI concerns stay outside the agent loop."""

    def state_changed(self, state: AgentState, plan: PlanManager) -> None:
        pass

    def tool_started(self, call: ToolCall) -> None:
        pass

    def tool_finished(self, call: ToolCall, result: ToolResult) -> None:
        pass

    def plan_changed(self, plan: PlanManager) -> None:
        pass

    def assistant_text(self, text: str) -> None:
        pass

    def warning(self, text: str) -> None:
        pass
