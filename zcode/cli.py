from __future__ import annotations

import argparse
import asyncio
import json
from uuid import uuid4
from dataclasses import replace
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from rich.console import Console

from zcode.config import Settings
from zcode.credentials import delete_api_key, prompt_and_save_api_key
from zcode.core.context import ContextManager
from zcode.core.controller import AgentController
from zcode.core.plan import PlanManager
from zcode.core.types import AgentState, ToolCall
from zcode.llm.deepseek import DeepSeekProvider
from zcode.tools.defaults import build_default_registry
from zcode.ui.renderer import RichRenderer
from zcode.workspace import Workspace


HELP = """[bold]Commands[/bold]
/help  Show this help
/plan  Show the current plan
/diff  Show changes made by the current task
/undo  Restore files changed by the current task
!cmd   Run a shell command directly (PowerShell on Windows)
/exit  Exit ZCode
"""

SLASH_COMMANDS = {
    "/help": "Show command help",
    "/plan": "Show the current plan",
    "/diff": "Show current-task changes",
    "/undo": "Restore current-task changes",
    "/exit": "Exit ZCode",
}


class SlashCommandCompleter(Completer):
    def get_completions(self, document: Document, complete_event):
        value = document.text_before_cursor
        if not value.startswith("/") or any(character.isspace() for character in value):
            return
        prefix = value.lower()
        for command, description in SLASH_COMMANDS.items():
            if command.startswith(prefix):
                yield Completion(
                    command,
                    start_position=-len(value),
                    display=command,
                    display_meta=description,
                )


STATUS_COLORS = {
    AgentState.READY: "ansibrightblack",
    AgentState.THINKING: "ansicyan",
    AgentState.PLANNING: "ansiblue",
    AgentState.EXECUTING: "ansimagenta",
    AgentState.RUNNING: "ansibrightblue",
    AgentState.VERIFYING: "ansigreen",
    AgentState.WAITING: "ansiyellow",
    AgentState.PAUSED: "ansiyellow",
    AgentState.COMPLETED: "ansigreen",
    AgentState.FAILED: "ansired",
}


def _truncate_display(value: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if get_cwidth(value) <= max_width:
        return value
    if max_width == 1:
        return "…"
    kept: list[str] = []
    width = 0
    for character in value:
        character_width = get_cwidth(character)
        if width + character_width > max_width - 1:
            break
        kept.append(character)
        width += character_width
    return "".join(kept) + "…"


def input_frame_top(width: int) -> str:
    frame_width = max(24, width)
    prefix = "╭─ ZCode "
    return prefix + "─" * max(1, frame_width - get_cwidth(prefix) - 1) + "╮\n│ "


def input_frame_bottom(
    primary: str,
    details: str,
    width: int,
) -> tuple[str, str, str]:
    frame_width = max(24, width)
    prefix = "╰─ "
    suffix = "╯"
    available = frame_width - get_cwidth(prefix) - get_cwidth(primary) - 3
    visible_details = _truncate_display(details, max(0, available))
    used = get_cwidth(prefix + primary + visible_details + " " + suffix)
    fill = "─" * max(1, frame_width - used)
    return prefix, primary + visible_details, " " + fill + suffix


def input_frame_close(width: int) -> str:
    frame_width = max(24, width)
    return "╰" + "─" * (frame_width - 2) + "╯"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ZCode local coding agent")
    parser.add_argument("workspace", nargs="?", default=".", help="Workspace directory")
    parser.add_argument("--model", help="Override ZCODE_MODEL")
    parser.add_argument(
        "--configure",
        action="store_true",
        help="Save or replace the DeepSeek API key in the system credential store",
    )
    parser.add_argument(
        "--clear-api-key",
        action="store_true",
        help="Remove the saved DeepSeek API key",
    )
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="Disable DeepSeek thinking mode",
    )
    return parser


async def run_cli(args: argparse.Namespace) -> int:
    console = Console()
    try:
        if args.clear_api_key:
            removed = delete_api_key()
            console.print(
                "[green]Saved API key removed.[/green]"
                if removed
                else "[dim]No saved API key was found.[/dim]"
            )
            return 0

        if args.configure:
            prompt_and_save_api_key(console)
            return 0

        settings = Settings.from_env(args.workspace)
        if not settings.api_key:
            settings = replace(settings, api_key=prompt_and_save_api_key(console))
        if args.model:
            settings = replace(settings, model=args.model)
        if args.no_thinking:
            settings = replace(settings, thinking="disabled")
        settings.require_api_key()
    except (EOFError, KeyboardInterrupt, OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, KeyboardInterrupt):
            console.print("\n[yellow]Setup cancelled.[/yellow]")
            return 130
        console.print(f"[red]{exc}[/red]")
        return 2

    workspace = Workspace(settings.workspace)
    plan = PlanManager()
    provider = DeepSeekProvider(settings)
    tools = build_default_registry(
        workspace,
        plan,
        command_timeout_seconds=settings.command_timeout_seconds,
        max_tool_output_chars=settings.max_tool_output_chars,
    )
    renderer = RichRenderer(workspace, console)
    context = ContextManager(
        max_chars=settings.max_context_chars,
        max_tool_output_chars=settings.max_tool_output_chars,
    )
    controller = AgentController(
        settings,
        provider,
        tools,
        workspace,
        plan,
        context=context,
        events=renderer,
    )

    history_dir = workspace.root / ".zcode"
    history_dir.mkdir(exist_ok=True)
    prompt_style = Style.from_dict(
        {
            "bottom-toolbar": "bg:#202020 #a0a0a0 noreverse",
            "input-border": "fg:ansicyan",
            **{
                f"status-{state.value}": f"fg:{color} bold"
                for state, color in STATUS_COLORS.items()
            },
        }
    )
    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_dir / "history")),
        style=prompt_style,
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
    )

    def bottom_toolbar() -> FormattedText:
        primary, details = renderer.status_parts(plan)
        prefix, status, suffix = input_frame_bottom(primary, details, console.width)
        state_class = f"class:bottom-toolbar class:status-{renderer.current_state.value}"
        return FormattedText(
            [
                ("class:bottom-toolbar class:input-border", prefix),
                (state_class, status),
                ("class:bottom-toolbar class:input-border", suffix),
            ]
        )

    def input_prompt() -> FormattedText:
        return FormattedText(
            [("class:input-border", input_frame_top(console.width))]
        )

    renderer.banner(settings.model)
    renderer.state_changed(AgentState.READY, plan)

    while True:
        try:
            value = (
                await session.prompt_async(
                    input_prompt,
                    bottom_toolbar=bottom_toolbar,
                    rprompt=FormattedText([("class:input-border", " │")]),
                )
            ).strip()
            console.print(input_frame_close(console.width), style="cyan dim")
        except EOFError:
            break
        except KeyboardInterrupt:
            console.print(input_frame_close(console.width), style="cyan dim")
            console.print("[yellow]Input cancelled.[/yellow]")
            continue

        if not value:
            continue
        if value == "/exit":
            break
        if value == "/help":
            console.print(HELP)
            continue
        if value == "/plan":
            renderer.plan_changed(plan)
            continue
        if value == "/diff":
            renderer.show_diff(workspace.diff())
            continue
        if value == "/undo":
            restored = workspace.undo()
            if restored:
                console.print("[green]Restored:[/green] " + ", ".join(restored))
            else:
                console.print("[dim]No current-task changes to restore.[/dim]")
            continue
        if value.startswith("!"):
            command = value[1:].strip()
            if not command:
                console.print("[yellow]Usage: !<command>, for example !ls[/yellow]")
                continue
            arguments = {"command": command}
            call = ToolCall(
                id=f"direct-shell-{uuid4().hex}",
                name="run_command",
                arguments=arguments,
                raw_arguments=json.dumps(arguments, ensure_ascii=False),
            )
            renderer.state_changed(AgentState.RUNNING, plan)
            renderer.start_status(plan)
            try:
                renderer.tool_started(call)
                result = await tools.execute(call)
                renderer.tool_finished(call, result)
                renderer.state_changed(
                    AgentState.COMPLETED if result.success else AgentState.FAILED,
                    plan,
                )
            finally:
                renderer.stop_status()
            continue
        if value.startswith("/"):
            console.print(f"[yellow]Unknown command: {value}[/yellow]")
            continue

        renderer.start_status(plan)
        try:
            try:
                await controller.run(value)
            except KeyboardInterrupt:
                controller.state = AgentState.PAUSED
                renderer.state_changed(AgentState.PAUSED, plan)
                renderer.warning("Task interrupted; current file changes were preserved.")
        finally:
            renderer.stop_status()

    console.print("[dim]Goodbye.[/dim]")
    return 0


def main() -> int:
    return asyncio.run(run_cli(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
