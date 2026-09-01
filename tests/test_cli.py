from __future__ import annotations

from io import StringIO

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.utils import get_cwidth
from rich.console import Console

from zcode.cli import (
    SlashCommandCompleter,
    input_frame_bottom,
    input_frame_close,
    input_frame_top,
    sessions_table,
    submitted_input_text,
)
from zcode.core.session import SessionSummary


def completions_for(value: str):
    return list(
        SlashCommandCompleter().get_completions(
            Document(value, cursor_position=len(value)), CompleteEvent()
        )
    )


def test_slash_completer_lists_commands_and_filters_prefix():
    all_commands = {item.text for item in completions_for("/")}
    help_matches = [item.text for item in completions_for("/he")]

    assert {"/help", "/plan", "/diff", "/undo", "/exit"} <= all_commands
    assert help_matches == ["/help"]


def test_slash_completer_ignores_natural_language_and_arguments():
    assert completions_for("help") == []
    assert completions_for("/help now") == []


def test_input_frame_has_fixed_width_and_truncates_long_status():
    width = 48
    top_lines = input_frame_top(width).splitlines()
    prefix, status, suffix = input_frame_bottom(
        "✓ completed",
        " │ cwd: a/very/long/path/that/does/not/fit │ git: main │ plan —",
        width,
    )

    assert len(top_lines) == 2
    assert len(top_lines[0]) == width
    assert "…" in status
    assert get_cwidth(prefix + status + suffix) == width
    assert get_cwidth(input_frame_close(width)) == width


def test_submitted_input_ignores_blank_and_renders_complete_box():
    assert submitted_input_text("   ", 48) is None

    rendered = submitted_input_text("/sessions", 48)

    assert rendered is not None
    lines = rendered.plain.splitlines()
    assert len(lines) == 3
    assert "/sessions" in lines[1]
    assert all(get_cwidth(line) == 48 for line in lines)


def test_submitted_input_wraps_without_losing_text():
    rendered = submitted_input_text("中文任务" * 10, 24)

    assert rendered is not None
    content = "".join(
        line[2:-2].rstrip() for line in rendered.plain.splitlines()[1:-1]
    )
    assert content == "中文任务" * 10


def test_sessions_table_aligns_columns_with_cjk_names():
    summaries = [
        SessionSummary(
            session_id="s-11111111",
            name="修复登录",
            updated_at="2026-09-01T01:02:03+00:00",
            cwd="src",
        ),
        SessionSummary(
            session_id="s-22222222",
            name="try",
            updated_at="2026-08-29T13:20:54+00:00",
            cwd=".",
        ),
    ]
    stream = StringIO()
    console = Console(file=stream, width=120, force_terminal=False)

    console.print(sessions_table(summaries, "s-11111111"))

    lines = stream.getvalue().splitlines()
    first = next(line for line in lines if "s-11111111" in line)
    second = next(line for line in lines if "s-22222222" in line)
    assert first.index("s-11111111") == second.index("s-22222222")
    first_cwd_column = get_cwidth(first[: first.index("src")])
    second_cwd_column = get_cwidth(second[: second.index(".")])
    assert first_cwd_column == second_cwd_column
    assert "●" in first
