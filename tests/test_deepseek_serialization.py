from __future__ import annotations

from types import SimpleNamespace

import pytest

from zcode.config import Settings
from zcode.core.types import Message, Role, ToolCall
from zcode.llm.deepseek import DeepSeekProvider


def test_deepseek_assistant_history_preserves_reasoning_and_tools():
    call = ToolCall("call-1", "read_file", {"path": "a.py"}, '{"path":"a.py"}')
    message = Message(
        Role.ASSISTANT,
        content="",
        reasoning_content="provider-owned reasoning",
        tool_calls=[call],
    )

    serialized = DeepSeekProvider._serialize_message(message)

    assert serialized["reasoning_content"] == "provider-owned reasoning"
    assert serialized["tool_calls"][0]["function"]["arguments"] == '{"path":"a.py"}'


def test_deepseek_tool_result_includes_call_id():
    serialized = DeepSeekProvider._serialize_message(
        Message(Role.TOOL, "contents", tool_call_id="call-1")
    )

    assert serialized == {
        "role": "tool",
        "content": "contents",
        "tool_call_id": "call-1",
    }


class FakeCompletions:
    def __init__(self):
        self.request = None

    async def create(self, **request):
        self.request = request
        function = SimpleNamespace(name="read_file", arguments='{bad json')
        tool_call = SimpleNamespace(id="call-1", function=function)
        message = SimpleNamespace(
            content="",
            reasoning_content="provider reasoning",
            tool_calls=[tool_call],
        )
        choice = SimpleNamespace(message=message, finish_reason="tool_calls")
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=4, total_tokens=14)
        return SimpleNamespace(choices=[choice], usage=usage)


@pytest.mark.asyncio
async def test_deepseek_generate_normalizes_response_and_invalid_json(tmp_path):
    completions = FakeCompletions()
    provider = DeepSeekProvider.__new__(DeepSeekProvider)
    provider.settings = Settings(workspace=tmp_path, api_key="test-key")
    provider.request_retries = 1
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )

    response = await provider.generate(
        [Message(Role.USER, "Read the file")],
        [],
    )

    assert completions.request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert response.assistant_message.reasoning_content == "provider reasoning"
    assert response.tool_calls[0].parse_error
    assert response.usage.total_tokens == 14
