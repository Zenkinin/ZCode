from __future__ import annotations

from abc import ABC, abstractmethod

from zcode.core.types import Message, ModelResponse, ToolDefinition


class LLMProvider(ABC):
    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> ModelResponse:
        """Convert provider output into ZCode's internal response type."""
