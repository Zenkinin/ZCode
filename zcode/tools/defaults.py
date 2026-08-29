from __future__ import annotations

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
from zcode.tools.shell import RunCommandTool
from zcode.workspace import Workspace


def build_default_registry(
    workspace: Workspace,
    plan: PlanManager,
    *,
    command_timeout_seconds: float = 120.0,
    max_tool_output_chars: int = 20_000,
) -> ToolRegistry:
    return ToolRegistry(
        [
            ChangeDirectoryTool(workspace),
            ListDirectoryTool(workspace),
            ReadFileTool(workspace),
            SearchTextTool(workspace),
            EditFileTool(workspace),
            WriteFileTool(workspace),
            RunCommandTool(workspace, command_timeout_seconds, max_tool_output_chars),
            CreatePlanTool(plan),
            UpdatePlanTool(plan),
        ]
    )
