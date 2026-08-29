from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from zcode.core.types import ToolDefinition, ToolResult


class Tool(ABC):
    definition: ToolDefinition
    activity: str = "executing"

    @abstractmethod
    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Execute validated tool arguments in the local environment."""
