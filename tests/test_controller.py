from __future__ import annotations

from collections import deque

import pytest

from zcode.config import Settings
from zcode.core.controller import AgentController
from zcode.core.plan import PlanManager
from zcode.core.types import (
    AgentState,
    Message,
    ModelResponse,
    Role,
    ToolCall,
)
from zcode.llm.base import LLMProvider
from zcode.tools.defaults import build_default_registry
from zcode.workspace import Workspace


class FakeProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = deque(responses)

    async def generate(self, messages, tools):
        item = self.responses[0] if len(self.responses) == 1 else self.responses.popleft()
        return item


def tool_response(call: ToolCall) -> ModelResponse:
    message = Message(Role.ASSISTANT, tool_calls=[call])
    return ModelResponse(None, [call], "tool_calls", message)


def final_response(text: str) -> ModelResponse:
    message = Message(Role.ASSISTANT, content=text)
    return ModelResponse(text, [], "stop", message)


def make_controller(tmp_path, provider, **overrides):
    settings = Settings(
        workspace=tmp_path,
        api_key="test-key",
        no_progress_limit=overrides.get("no_progress_limit", 3),
        emergency_max_steps=overrides.get("emergency_max_steps", 10),
    )
    workspace = Workspace(tmp_path)
    plan = PlanManager()
    tools = build_default_registry(workspace, plan)
    return AgentController(settings, provider, tools, workspace, plan), workspace


@pytest.mark.asyncio
async def test_controller_executes_tool_then_finishes(tmp_path):
    call = ToolCall(
        "call-1",
        "write_file",
        {"path": "hello.py", "content": "print('hello')\n"},
        '{"path":"hello.py","content":"print(\\"hello\\")\\n"}',
    )
    controller, _ = make_controller(
        tmp_path, FakeProvider([tool_response(call), final_response("Done")])
    )

    outcome = await controller.run("Create hello.py")

    assert outcome.state == AgentState.COMPLETED
    assert outcome.text == "Done"
    assert (tmp_path / "hello.py").exists()


@pytest.mark.asyncio
async def test_controller_changes_session_directory_before_relative_write(tmp_path):
    (tmp_path / "OPPO 互联").mkdir()
    change = ToolCall(
        "call-cd",
        "change_directory",
        {"path": "OPPO 互联"},
        '{"path":"OPPO 互联"}',
    )
    write = ToolCall(
        "call-write",
        "write_file",
        {"path": "README.md", "content": "# Today\n"},
        '{"path":"README.md","content":"# Today\\n"}',
    )
    controller, workspace = make_controller(
        tmp_path,
        FakeProvider(
            [tool_response(change), tool_response(write), final_response("Done")]
        ),
    )

    outcome = await controller.run("Switch to OPPO and create README.md")

    assert outcome.state == AgentState.COMPLETED
    assert workspace.cwd_relative == "OPPO 互联"
    assert (tmp_path / "OPPO 互联" / "README.md").exists()
    assert not (tmp_path / "README.md").exists()


@pytest.mark.asyncio
async def test_controller_pauses_repeated_no_progress_call(tmp_path):
    call = ToolCall(
        "call-repeat",
        "list_directory",
        {"path": "."},
        '{"path":"."}',
    )
    controller, _ = make_controller(
        tmp_path,
        FakeProvider([tool_response(call)]),
        no_progress_limit=3,
        emergency_max_steps=8,
    )

    outcome = await controller.run("Keep listing forever")

    assert outcome.state == AgentState.WAITING
    assert "no-progress" in outcome.text


@pytest.mark.asyncio
async def test_controller_reminds_once_before_leaving_plan_incomplete(tmp_path):
    create = ToolCall(
        "plan-1",
        "create_plan",
        {"steps": ["Inspect", "Verify"]},
        '{"steps":["Inspect","Verify"]}',
    )
    provider = FakeProvider(
        [tool_response(create), final_response("Done early"), final_response("Need input")]
    )
    controller, _ = make_controller(tmp_path, provider)

    outcome = await controller.run("Do a complex task")

    assert outcome.state == AgentState.WAITING
    assert outcome.text == "Need input"


def test_controller_reinjects_current_task_and_plan(tmp_path):
    controller, _ = make_controller(tmp_path, FakeProvider([final_response("unused")]))
    controller._current_task = "Fix the parser"
    controller.plan.create(["Inspect", "Verify"])

    messages = controller._model_messages()

    assert messages[1].role == Role.SYSTEM
    assert "Fix the parser" in messages[1].content
    assert "Inspect" in messages[1].content
