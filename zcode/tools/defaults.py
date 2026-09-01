from __future__ import annotations

from typing import Awaitable, Callable

from zcode.core.plan import PlanManager
from zcode.tools.filesystem import (
    EditFileTool,
    ListDirectoryTool,
    ReadFileTool,
    SearchTextTool,
    WriteFileTool,
)
from zcode.tools.planning import CreatePlanTool, UpdatePlanTool
from zcode.tools.navigation import ChangeDirectoryTool
from zcode.tools.registry import ToolRegistry
from zcode.tools.shell import CommandRisk, RunCommandTool
from zcode.workspace import Workspace


def build_default_registry(
    workspace: Workspace,
    plan: PlanManager,
    *,
    command_timeout_seconds: float = 120.0,
    max_tool_output_chars: int = 20_000,
    confirm_callback: Callable[[str, CommandRisk, str], Awaitable[str]] | None = None,
) -> ToolRegistry:
    return ToolRegistry(
        [
            ChangeDirectoryTool(workspace),
            ListDirectoryTool(workspace),
            ReadFileTool(workspace),
            SearchTextTool(workspace),
            EditFileTool(workspace),
            WriteFileTool(workspace),
            RunCommandTool(
                workspace,
                command_timeout_seconds,
                max_tool_output_chars,
                confirm_callback=confirm_callback,
            ),
            CreatePlanTool(plan),
            UpdatePlanTool(plan),
        ]
    )
