from __future__ import annotations

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.utils import get_cwidth

from zcode.cli import (
    SlashCommandCompleter,
    input_frame_bottom,
    input_frame_close,
    input_frame_top,
)


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
