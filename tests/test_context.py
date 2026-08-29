from __future__ import annotations

from zcode.core.context import ContextManager
from zcode.core.types import Message, Role, ToolCall


def test_context_truncates_tool_output_from_middle():
    context = ContextManager(max_tool_output_chars=20)
    context.add(Message(Role.TOOL, "a" * 100, tool_call_id="call-1"))

    assert "omitted" in context.messages[0].content
    assert len(context.messages[0].content) < 100


def test_context_keeps_assistant_tool_result_group_together():
    context = ContextManager(max_chars=100)
    context.add(Message(Role.SYSTEM, "system"))
    context.add(Message(Role.USER, "original task"))
    context.add(Message(Role.USER, "x" * 120))
    call = ToolCall("call-1", "read_file", {"path": "a.py"}, '{"path":"a.py"}')
    context.add(Message(Role.ASSISTANT, tool_calls=[call]))
    context.add(Message(Role.TOOL, "result", tool_call_id="call-1"))

    bounded = context.build()
    roles = [message.role for message in bounded]
    assert roles[:2] == [Role.SYSTEM, Role.USER]
    assert Role.ASSISTANT in roles
    assistant_index = roles.index(Role.ASSISTANT)
    assert roles[assistant_index + 1] == Role.TOOL
