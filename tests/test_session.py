from __future__ import annotations

from zcode.core.plan import PlanManager
from zcode.core.session import (
    DEFAULT_NAME_SOURCE,
    USER_NAME_SOURCE,
    SessionCorrupt,
    SessionSnapshot,
    SessionStore,
    clean_session_name,
    derive_session_name,
    plan_to_records,
)
from zcode.core.types import Message, Role, ToolCall


def test_session_store_round_trip_and_active_selection(tmp_path):
    store = SessionStore(tmp_path)
    first = store.create("first")
    second = store.create("second")
    second.cwd = "src"
    second.messages = [
        Message(Role.USER, "Remember this"),
        Message(
            Role.ASSISTANT,
            tool_calls=[ToolCall("call-1", "read_file", {"path": "x.py"}, '{"path":"x.py"}')],
            reasoning_content="internal protocol field",
        ),
        Message(Role.TOOL, "contents", tool_call_id="call-1"),
    ]
    plan = PlanManager()
    plan.create(["Inspect"])
    second.plan = plan_to_records(plan.steps)
    store.save(second, active=True)

    loaded = store.load_active()

    assert loaded is not None
    assert loaded.session_id == second.session_id
    assert loaded.name == "second"
    assert loaded.name_source == USER_NAME_SOURCE
    assert loaded.cwd == "src"
    assert loaded.messages[1].tool_calls[0].name == "read_file"
    assert loaded.plan[0]["description"] == "Inspect"
    assert {item.name for item in store.list()} == {"first", "second"}
    assert (tmp_path / ".zcode" / "sessions" / f"{first.session_id}.jsonl").exists()


def test_corrupt_session_is_reported_without_key_material(tmp_path):
    store = SessionStore(tmp_path)
    snapshot = store.create("broken")
    path = tmp_path / ".zcode" / "sessions" / f"{snapshot.session_id}.jsonl"
    path.write_text("not json\n", encoding="utf-8")

    try:
        store.load(snapshot.session_id)
    except SessionCorrupt as exc:
        assert "API" not in str(exc)
    else:
        raise AssertionError("corrupt session should be rejected")


def test_default_and_derived_session_names(tmp_path):
    store = SessionStore(tmp_path)
    snapshot = store.create()

    assert snapshot.name.startswith("session-")
    assert snapshot.name_source == DEFAULT_NAME_SOURCE
    assert derive_session_name("请你帮我 修复登录页面的失败测试。") == "修复登录页面的失败测试"
    long_name = derive_session_name(
        "实现一个非常长的任务描述，用来确认会话名称会被安全地截断而不会无限增长"
    )
    assert long_name.endswith("…")


def test_session_name_normalization_and_limit():
    assert clean_session_name("  修复\n 登录问题  ") == "修复 登录问题"

    try:
        clean_session_name("x" * 41)
    except ValueError as exc:
        assert "40" in str(exc)
    else:
        raise AssertionError("overlong session names should be rejected")
