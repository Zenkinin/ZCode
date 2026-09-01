from __future__ import annotations

from io import StringIO

from rich.console import Console

from zcode.core.plan import PlanManager
from zcode.core.types import AgentState, ToolCall, ToolResult
from zcode.ui.renderer import RichRenderer
from zcode.workspace import Workspace


def test_renderer_status_contains_state_git_and_plan(tmp_path):
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, width=120)
    plan = PlanManager()
    plan.create(["Inspect", "Verify"])
    renderer = RichRenderer(tmp_path, console)

    renderer.state_changed(AgentState.EXECUTING, plan)

    output = renderer.status_text(plan).plain
    assert "executing" in output
    assert "git:" in output
    assert "plan 0/2" in output
    assert stream.getvalue() == ""


def test_renderer_treats_tool_output_as_plain_text(tmp_path):
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, width=120)
    renderer = RichRenderer(tmp_path, console)
    call = ToolCall("call-1", "run_command", {"command": "dir"}, "{}")

    renderer.tool_started(call)
    renderer.tool_finished(
        call,
        ToolResult(True, "exit_code: 0\nstdout:\n[folder] <DIR> file_name"),
    )

    output = stream.getvalue()
    assert "[folder] <DIR> file_name" in output


def test_renderer_collapses_and_restores_error_output(tmp_path):
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, width=120)
    renderer = RichRenderer(tmp_path, console)
    call = ToolCall("call-1", "run_command", {"command": "tests"}, "{}")

    renderer.tool_finished(
        call,
        ToolResult(
            False,
            "exit_code: 1\nstdout:\nfirst\nsecond\nthird\nfourth\nstderr:\nsk-secret-example123456",
            metadata={"exit_code": 1},
        ),
    )

    collapsed = stream.getvalue()
    assert "error e-001" in collapsed
    assert "Use /error e-001" in collapsed
    assert "fourth" not in collapsed
    assert renderer.error_records[0]["content"].endswith("sk-[REDACTED]")

    stream.seek(0)
    stream.truncate(0)
    assert renderer.show_error("e-001")
    assert "fourth" in stream.getvalue()


def test_renderer_status_uses_session_working_directory(tmp_path):
    (tmp_path / "OPPO 互联").mkdir()
    workspace = Workspace(tmp_path)
    workspace.change_directory("OPPO 互联")
    renderer = RichRenderer(workspace, Console(file=StringIO(), force_terminal=False))

    assert "cwd: OPPO 互联" in renderer.status_text().plain


def test_renderer_animates_only_active_states(tmp_path):
    renderer = RichRenderer(tmp_path, Console(file=StringIO(), force_terminal=False))
    plan = PlanManager()

    renderer.state_changed(AgentState.THINKING, plan)
    first, _ = renderer.status_parts(spinner_frame=0)
    second, _ = renderer.status_parts(spinner_frame=1)
    assert first != second
    assert first.endswith(" thinking")

    renderer.state_changed(AgentState.COMPLETED, plan)
    completed_first, _ = renderer.status_parts(spinner_frame=0)
    completed_second, _ = renderer.status_parts(spinner_frame=1)
    assert completed_first == completed_second == "✓ completed"
