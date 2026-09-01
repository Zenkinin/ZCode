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
from rich.table import Table
from rich.text import Text

from zcode.config import Settings
from zcode.credentials import delete_api_key, prompt_and_save_api_key
from zcode.core.context import ContextManager
from zcode.core.controller import AgentController
from zcode.core.plan import PlanManager
from zcode.core.session import (
    DEFAULT_NAME_SOURCE,
    DERIVED_NAME_SOURCE,
    USER_NAME_SOURCE,
    SessionCorrupt,
    SessionSnapshot,
    SessionStore,
    SessionSummary,
    clean_session_name,
    derive_session_name,
    plan_to_records,
)
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
/new [name]  Create a new conversation session
/sessions  List sessions in this workspace
/switch <id>  Switch to a session
/session  Show the current session
/rename <name>  Rename the current session
!cmd   Run a shell command directly (PowerShell on Windows)
/exit  Exit ZCode
"""

SLASH_COMMANDS = {
    "/help": "Show command help",
    "/plan": "Show the current plan",
    "/diff": "Show current-task changes",
    "/undo": "Restore current-task changes",
    "/new": "Create a new conversation session",
    "/sessions": "List workspace sessions",
    "/switch": "Switch to a session",
    "/session": "Show current session",
    "/rename": "Rename the current session",
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


def sessions_table(
    summaries: list[SessionSummary],
    active_session_id: str,
) -> Table:
    """Build a compact, display-width-aware session list."""
    table = Table(
        box=None,
        show_edge=False,
        pad_edge=False,
        collapse_padding=True,
        padding=(0, 2),
    )
    table.add_column("", width=1, no_wrap=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("NAME", min_width=8, max_width=32, overflow="ellipsis", no_wrap=True)
    table.add_column("CWD", min_width=3, max_width=40, overflow="ellipsis", no_wrap=True)
    table.add_column("UPDATED", style="dim", no_wrap=True)
    for summary in summaries:
        marker = (
            Text("●", style="green")
            if summary.session_id == active_session_id
            else Text(" ")
        )
        table.add_row(
            marker,
            summary.session_id,
            summary.name,
            summary.cwd,
            summary.updated_at,
        )
    return table


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
    session_store = SessionStore(workspace.root)
    try:
        active_session = session_store.load_active()
    except SessionCorrupt as exc:
        console.print(f"[yellow]Ignoring corrupt saved session: {exc}[/yellow]")
        active_session = None
    if active_session is None:
        active_session = session_store.create()

    def restore_workspace_state(snapshot: SessionSnapshot) -> None:
        workspace.cwd = workspace.root
        if snapshot.cwd != ".":
            try:
                workspace.change_directory(snapshot.cwd)
            except (OSError, ValueError):
                snapshot.cwd = "."

    restore_workspace_state(active_session)
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
    controller.restore_session(active_session.messages, active_session.plan)

    def save_current_session() -> None:
        active_session.cwd = workspace.cwd_relative
        active_session.messages = [
            message
            for message in controller.context.messages
            if message.role.value != "system"
        ]
        active_session.plan = plan_to_records(plan.steps)
        try:
            session_store.save(active_session, active=True)
        except OSError as exc:
            console.print(f"[yellow]Could not save session: {exc}[/yellow]")

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
            save_current_session()
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
        if value == "/session":
            console.print(
                f"Session {active_session.session_id} · {active_session.name} · "
                f"cwd: {workspace.cwd_relative}"
            )
            continue
        if value == "/sessions":
            summaries = session_store.list()
            if not summaries:
                console.print("[dim]No sessions in this workspace.[/dim]")
            else:
                console.print(sessions_table(summaries, active_session.session_id))
            continue
        if value == "/rename" or value.startswith("/rename "):
            requested_name = value[7:].strip()
            if not requested_name:
                console.print("[yellow]Usage: /rename <name>[/yellow]")
                continue
            try:
                active_session.name = clean_session_name(requested_name)
            except ValueError as exc:
                console.print(f"[yellow]{exc}[/yellow]")
                continue
            active_session.name_source = USER_NAME_SOURCE
            save_current_session()
            console.print(f"[green]Session renamed: {active_session.name}[/green]")
            continue
        if value == "/new" or value.startswith("/new "):
            save_current_session()
            requested_name = value[4:].strip() or None
            try:
                active_session = session_store.create(requested_name)
            except ValueError as exc:
                console.print(f"[yellow]{exc}[/yellow]")
                continue
            workspace.cwd = workspace.root
            controller.reset_session()
            renderer.state_changed(AgentState.READY, plan)
            console.print(f"[green]New session: {active_session.session_id} · {active_session.name}[/green]")
            continue
        if value == "/switch" or value.startswith("/switch "):
            session_id = value[7:].strip()
            if not session_id:
                console.print("[yellow]Usage: /switch <session-id>[/yellow]")
                continue
            try:
                target_session = session_store.load(session_id)
            except (FileNotFoundError, SessionCorrupt, ValueError) as exc:
                console.print(f"[red]Could not switch session: {exc}[/red]")
                continue
            save_current_session()
            active_session = target_session
            restore_workspace_state(active_session)
            controller.restore_session(active_session.messages, active_session.plan)
            session_store.set_active(active_session.session_id)
            renderer.state_changed(AgentState.READY, plan)
            console.print(f"[green]Switched to: {active_session.session_id} · {active_session.name}[/green]")
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

        if active_session.name_source == DEFAULT_NAME_SOURCE:
            active_session.name = derive_session_name(value)
            active_session.name_source = DERIVED_NAME_SOURCE

        renderer.start_status(plan)
        try:
            try:
                await controller.run(value)
            except KeyboardInterrupt:
                controller.state = AgentState.PAUSED
                renderer.state_changed(AgentState.PAUSED, plan)
                renderer.warning("Task interrupted; current file changes were preserved.")
        finally:
            save_current_session()
            renderer.stop_status()

    console.print("[dim]Goodbye.[/dim]")
    return 0


def main() -> int:
    return asyncio.run(run_cli(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
