from __future__ import annotations

from typing import Any

from zcode.core.types import ToolDefinition, ToolResult
from zcode.tools.base import Tool
from zcode.workspace import Workspace


class ChangeDirectoryTool(Tool):
    activity = "executing"

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.definition = ToolDefinition(
            name="change_directory",
            description=(
                "Persistently change ZCode's session working directory to a directory "
                "inside the workspace. Call this whenever the user asks to switch or "
                "move into a directory. All later relative file paths and shell commands "
                "start there."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the current session directory.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("'path' must be a non-empty string")
        self.workspace.change_directory(path)
        return ToolResult(
            True,
            f"Working directory changed to: {self.workspace.cwd_relative}",
            metadata={"cwd_changed": True, "cwd": self.workspace.cwd_relative},
        )
