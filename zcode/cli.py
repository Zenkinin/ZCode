from __future__ import annotations

import argparse
import asyncio
import json
import re
from uuid import uuid4
from dataclasses import replace
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.shortcuts import yes_no_dialog
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
from zcode.tools.shell import classify_command
from zcode.ui.renderer import RichRenderer
from zcode.workspace import Workspace


HELP = """[bold]Commands[/bold]
/help  Show this help
/plan  Show the current plan
/diff  Show changes made by the current task
/undo  Restore files changed by the current task
/new [name]  Create a new conversation session
/sessions  List sessions in this workspace
/switch [id]  Select or switch to a session
/session  Show the current session
/rename <name>  Rename the current session
/cwd  Show workspace, cwd, and Git root
/cd <path>  Change the persistent session directory
/error [id]  Show full output for an error
/errors  List errors in the current session
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
    "/switch": "Select or switch to a session",
    "/session": "Show current session",
    "/rename": "Rename the current session",
    "/cwd": "Show current directories",
    "/cd": "Change persistent session directory",
    "/error": "Show full error output",
    "/errors": "List session errors",
    "/exit": "Exit ZCode",
}

MAX_VISIBLE_COMPLETIONS = 8
PREFERRED_INPUT_BODY_ROWS = 8


def input_body_rows(terminal_height: int) -> int:
    """Keep the prompt above the terminal edge without overwhelming short terminals."""
    return min(PREFERRED_INPUT_BODY_ROWS, max(4, terminal_height // 4))


class SlashCommandCompleter(Completer):
    def __init__(self, session_provider=None, active_session_provider=None) -> None:
        self.session_provider = session_provider or (lambda: [])
        self.active_session_provider = active_session_provider or (lambda: "")

    def get_completions(self, document: Document, complete_event):
        value = document.text_before_cursor
        if value.startswith("/switch "):
            query = value[8:].strip().lower()
            active_id = self.active_session_provider()
            emitted = 0
            for summary in self.session_provider():
                searchable = f"{summary.session_id} {summary.name} {summary.cwd}".lower()
                if query and query not in searchable:
                    continue
                marker = "● " if summary.session_id == active_id else ""
                yield Completion(
                    summary.session_id,
                    start_position=-len(value[8:]),
                    display=_truncate_display(f"{marker}{summary.name}", 30),
                    display_meta=_truncate_display(
                        f"{summary.session_id} · {summary.cwd}", 36
                    ),
                )
                emitted += 1
                if emitted >= MAX_VISIBLE_COMPLETIONS:
                    break
            return
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


def submitted_input_text(value: str, width: int) -> Text | None:
    """Render a completed input box; blank submissions leave no scrollback."""
    value = value.strip()
    if not value:
        return None

    frame_width = max(24, width)
    content_width = frame_width - 4
    rows: list[str] = []
    current: list[str] = []
    current_width = 0
    for character in value:
        character_width = get_cwidth(character)
        if current and current_width + character_width > content_width:
            rows.append("".join(current))
            current = []
            current_width = 0
        current.append(character)
        current_width += character_width
    rows.append("".join(current))

    rendered = Text()
    top = input_frame_top(frame_width).splitlines()[0]
    rendered.append(top + "\n", style="cyan dim")
    for row in rows:
        padding = " " * max(0, content_width - get_cwidth(row))
        rendered.append("│ ", style="cyan dim")
        rendered.append(row)
        rendered.append(padding + " │\n", style="cyan dim")
    rendered.append(input_frame_close(frame_width), style="cyan dim")
    return rendered


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


def session_choices(
    summaries: list[SessionSummary], active_session_id: str
) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    for summary in summaries:
        marker = "●" if summary.session_id == active_session_id else " "
        label = (
            f"{marker} {summary.name[:28]:<28}  {summary.session_id}  "
            f"{summary.cwd[:28]:<28}  {summary.updated_at[:16]}"
        )
        choices.append((summary.session_id, label))
    return choices


def persistent_cd_target(command: str) -> str | None:
    """Return a path for a simple cd command; reject compound shell syntax."""
    stripped = command.strip()
    if any(operator in stripped for operator in (";", "|", "&&", "||")):
        return None
    match = re.fullmatch(r"(?i)(?:cd|set-location)\s+(.+)", stripped)
    if not match:
        return None
    target = match.group(1).strip()
    if len(target) >= 2 and target[0] == target[-1] and target[0] in {'\"', "'"}:
        target = target[1:-1]
    return target or None


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
    renderer.restore_errors(active_session.errors)

    def save_current_session() -> None:
        active_session.cwd = workspace.cwd_relative
        active_session.messages = [
            message
            for message in controller.context.messages
            if message.role.value != "system"
        ]
        active_session.plan = plan_to_records(plan.steps)
        active_session.errors = [dict(record) for record in renderer.error_records]
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
            "completion-menu": "bg:#303030 #f0f0f0",
            "completion-menu.completion": "bg:#303030 #f0f0f0",
            "completion-menu.completion.current": "bg:#008080 #ffffff bold",
            "completion-menu.meta.completion": "bg:#252525 #b0b0b0",
            "completion-menu.meta.completion.current": "bg:#006060 #ffffff",
            **{
                f"status-{state.value}": f"fg:{color} bold"
                for state, color in STATUS_COLORS.items()
            },
        }
    )
    key_bindings = KeyBindings()

    @key_bindings.add("enter")
    def submit_non_blank(event) -> None:
        if event.current_buffer.text.strip():
            event.current_buffer.validate_and_handle()

    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_dir / "history")),
        style=prompt_style,
        completer=SlashCommandCompleter(
            session_provider=session_store.list,
            active_session_provider=lambda: active_session.session_id,
        ),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        erase_when_done=False,
        key_bindings=key_bindings,
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
                    # PromptSession reserves this height even before completion opens
                    # because complete_while_typing is enabled. This keeps the input
                    # cursor fixed near the top of a stable input area instead of
                    # letting it fall onto the terminal's final row.
                    reserve_space_for_menu=input_body_rows(console.height),
                )
            ).strip()
        except EOFError:
            break
        except KeyboardInterrupt:
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
        if value == "/cwd":
            git_root = workspace.root
            console.print(
                f"workspace: {workspace.root}\n"
                f"cwd:       {workspace.cwd_relative}\n"
                f"git root:  {git_root}"
            )
            continue
        if value == "/cd" or value.startswith("/cd "):
            target = value[3:].strip()
            if not target:
                console.print("[yellow]Usage: /cd <path>[/yellow]")
                continue
            try:
                workspace.change_directory(target)
            except (OSError, ValueError) as exc:
                console.print(f"[red]Could not change directory: {exc}[/red]")
                continue
            save_current_session()
            renderer.state_changed(AgentState.READY, plan)
            console.print(f"[green]cwd: {workspace.cwd_relative}[/green]")
            continue
        if value == "/error" or value.startswith("/error "):
            error_id = value[6:].strip() or None
            if not renderer.show_error(error_id):
                message = f"Error not found: {error_id}" if error_id else "No errors recorded."
                console.print(f"[yellow]{message}[/yellow]")
            continue
        if value == "/errors":
            renderer.show_errors()
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
            renderer.restore_errors([])
            renderer.state_changed(AgentState.READY, plan)
            console.print(f"[green]New session: {active_session.session_id} · {active_session.name}[/green]")
            continue
        if value == "/switch" or value.startswith("/switch "):
            session_id = value[7:].strip()
            if not session_id:
                summaries = session_store.list()
                if len(summaries) <= 1:
                    console.print("[dim]No other sessions in this workspace.[/dim]")
                else:
                    console.print(sessions_table(summaries, active_session.session_id))
                    console.print(
                        "[dim]Type /switch followed by a space, then use ↑/↓ and Enter.[/dim]"
                    )
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
            renderer.restore_errors(active_session.errors)
            session_store.set_active(active_session.session_id)
            renderer.state_changed(AgentState.READY, plan)
            console.print(f"[green]Switched to: {active_session.session_id} · {active_session.name}[/green]")
            continue
        if value.startswith("!"):
            command = value[1:].strip()
            if not command:
                console.print("[yellow]Usage: !<command>, for example !ls[/yellow]")
                continue
            cd_target = persistent_cd_target(command)
            if cd_target is not None:
                try:
                    workspace.change_directory(cd_target)
                except (OSError, ValueError) as exc:
                    console.print(f"[red]Could not change directory: {exc}[/red]")
                    continue
                save_current_session()
                renderer.state_changed(AgentState.READY, plan)
                console.print(f"[green]cwd: {workspace.cwd_relative}[/green]")
                continue
            if re.match(r"(?i)^\s*(?:cd|set-location)\b", command):
                console.print(
                    "[yellow]Compound cd commands do not persist. "
                    "Use /cd <path>, then run the command separately.[/yellow]"
                )
                continue
            arguments = {"command": command}
            risk = classify_command(command)
            if risk.level == "destructive":
                confirmed = await yes_no_dialog(
                    title="Confirm destructive command",
                    text=(
                        f"Risk: {risk.reason}\n\n"
                        f"cwd: {workspace.cwd}\n\n"
                        f"{command}\n\nRun this command?"
                    ),
                    yes_text="Run",
                    no_text="Cancel",
                ).run_async()
                if not confirmed:
                    console.print("[yellow]Command cancelled.[/yellow]")
                    continue
                arguments["_confirmed"] = True
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
