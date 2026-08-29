from __future__ import annotations

import asyncio
import json
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from zcode.config import Settings
from zcode.core.types import Message, ModelResponse, Role, ToolCall, ToolDefinition, Usage
from zcode.llm.base import LLMProvider


class DeepSeekProvider(LLMProvider):
    """DeepSeek Chat Completions adapter using the official OpenAI-compatible format."""

    def __init__(self, settings: Settings, *, request_retries: int = 3) -> None:
        settings.require_api_key()
        self.settings = settings
        self.request_retries = request_retries
        self.client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            max_retries=0,
            timeout=60.0,
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
    ) -> ModelResponse:
        request: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [self._serialize_message(message) for message in messages],
            "tools": [tool.as_openai_tool() for tool in tools],
            "tool_choice": "auto",
            "reasoning_effort": self.settings.reasoning_effort,
            "extra_body": {"thinking": {"type": self.settings.thinking}},
        }

        response = None
        retryable = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)
        for attempt in range(self.request_retries):
            try:
                response = await self.client.chat.completions.create(**request)
                break
            except retryable:
                if attempt + 1 >= self.request_retries:
                    raise
                await asyncio.sleep(0.5 * (2**attempt))
        if response is None or not response.choices:
            raise RuntimeError("DeepSeek returned an empty response")

        choice = response.choices[0]
        provider_message = choice.message
        calls: list[ToolCall] = []
        for item in provider_message.tool_calls or []:
            raw_arguments = item.function.arguments or "{}"
            parse_error: str | None = None
            try:
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must decode to an object")
            except (json.JSONDecodeError, ValueError) as exc:
                arguments = {}
                parse_error = str(exc)
            calls.append(
                ToolCall(
                    id=item.id,
                    name=item.function.name,
                    arguments=arguments,
                    raw_arguments=raw_arguments,
                    parse_error=parse_error,
                )
            )

        reasoning_content = getattr(provider_message, "reasoning_content", None)
        assistant_message = Message(
            role=Role.ASSISTANT,
            content=provider_message.content,
            tool_calls=calls,
            reasoning_content=reasoning_content,
        )
        raw_usage = response.usage
        usage = Usage(
            input_tokens=getattr(raw_usage, "prompt_tokens", None),
            output_tokens=getattr(raw_usage, "completion_tokens", None),
            total_tokens=getattr(raw_usage, "total_tokens", None),
        )
        return ModelResponse(
            text=provider_message.content,
            tool_calls=calls,
            finish_reason=choice.finish_reason,
            assistant_message=assistant_message,
            usage=usage,
        )

    @staticmethod
    def _serialize_message(message: Message) -> dict[str, Any]:
        data: dict[str, Any] = {"role": message.role.value, "content": message.content}
        if message.role == Role.ASSISTANT:
            if message.reasoning_content is not None:
                data["reasoning_content"] = message.reasoning_content
            if message.tool_calls:
                data["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.raw_arguments,
                        },
                    }
                    for call in message.tool_calls
                ]
        if message.role == Role.TOOL:
            data["tool_call_id"] = message.tool_call_id
        return data
