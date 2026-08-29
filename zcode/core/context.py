from __future__ import annotations

from dataclasses import dataclass, field

from zcode.core.types import Message, Role


def _message_size(message: Message) -> int:
    size = len(message.content or "") + len(message.reasoning_content or "")
    return size + sum(len(call.raw_arguments) + len(call.name) for call in message.tool_calls)


@dataclass(slots=True)
class ContextManager:
    max_chars: int = 180_000
    max_tool_output_chars: int = 20_000
    messages: list[Message] = field(default_factory=list)

    def add(self, message: Message) -> None:
        if message.role == Role.TOOL and message.content:
            message.content = self._truncate_tool_output(message.content)
        self.messages.append(message)

    def reset(self, system_prompt: str) -> None:
        self.messages = [Message(Role.SYSTEM, system_prompt)]

    def build(self) -> list[Message]:
        """Return a bounded history without splitting assistant/tool protocol groups."""
        blocks = self._group_messages()
        if sum(sum(_message_size(item) for item in block) for block in blocks) <= self.max_chars:
            return list(self.messages)

        pinned: list[list[Message]] = []
        remaining = list(blocks)
        if remaining and remaining[0][0].role == Role.SYSTEM:
            pinned.append(remaining.pop(0))
        first_user_index = next(
            (index for index, block in enumerate(remaining) if block[0].role == Role.USER),
            None,
        )
        if first_user_index is not None:
            pinned.append(remaining.pop(first_user_index))

        selected = list(pinned)
        used = sum(sum(_message_size(item) for item in block) for block in selected)
        recent_added = False
        for block in reversed(remaining):
            block_size = sum(_message_size(item) for item in block)
            if recent_added and used + block_size > self.max_chars:
                continue
            selected.insert(len(pinned), block)
            used += block_size
            recent_added = True
        return [message for block in selected for message in block]

    def _group_messages(self) -> list[list[Message]]:
        blocks: list[list[Message]] = []
        index = 0
        while index < len(self.messages):
            message = self.messages[index]
            block = [message]
            index += 1
            if message.role == Role.ASSISTANT and message.tool_calls:
                expected = {call.id for call in message.tool_calls}
                while index < len(self.messages):
                    candidate = self.messages[index]
                    if candidate.role != Role.TOOL or candidate.tool_call_id not in expected:
                        break
                    block.append(candidate)
                    index += 1
            blocks.append(block)
        return blocks

    def _truncate_tool_output(self, value: str) -> str:
        limit = self.max_tool_output_chars
        if len(value) <= limit:
            return value
        half = max(1, (limit - 80) // 2)
        omitted = len(value) - half * 2
        return f"{value[:half]}\n[... {omitted} characters omitted ...]\n{value[-half:]}"
