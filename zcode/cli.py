from __future__ import annotations

import argparse
import asyncio
import hashlib
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
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.filters import Condition
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
from zcode.security import Approval, SecurityPolicy, SecurityPolicyError
from zcode.tools.shell import classify_command, command_approval_scope
from zcode.ui.renderer import RichRenderer
from zcode.workspace import Workspace


HELP_COMMANDS = (
    ("/help", "Show this help"),
    ("/plan", "Show the current plan"),
    ("/diff", "Show current-task changes"),
    ("/model", "Show model and thinking settings"),
    ("/model <name>", "Switch model for later requests"),
    ("/model thinking <off|low|high|max>", "Set DeepSeek thinking mode"),
    ("/new [name]", "Create a new conversation session"),
    ("/sessions", "List sessions in this workspace"),
    ("/switch [id]", "Select or switch to a session"),
    ("/rename <name>", "Rename the current session"),
    ("/clear", "Clear terminal output"),
    ("/continue", "Continue a paused task with corrections"),
    ("/error [id]", "Show full output for an error"),
    ("/errors", "List errors in the current session"),
    ("/safety", "View session and permanent shell approvals"),
    ("!cmd", "Run PowerShell directly without the model"),
    ("/exit", "Exit ZCode"),
)

SLASH_COMMANDS = {
    "/help": "Show command help",
    "/plan": "Show the current plan",
    "/diff": "Show current-task changes",
    "/model": "Switch model or thinking mode",
    "/new": "Create a new conversation session",
    "/sessions": "List workspace sessions",
    "/switch": "Select or switch to a session",
    "/rename": "Rename the current session",
    "/clear": "Clear terminal output",
    "/continue": "Continue paused task",
    "/error": "Show full error output",
    "/errors": "List session errors",
    "/safety": "View session and permanent shell approvals",
    "/exit": "Exit ZCode",
}

MAX_VISIBLE_COMPLETIONS = 8
PREFERRED_INPUT_BODY_ROWS = 8
KNOWN_DEEPSEEK_MODELS = (
    "deepseek-v4-flash",
    "deepseek-chat",
    "deepseek-reasoner",
)
THINKING_LEVELS = ("off", "low", "high", "max")


def input_body_rows(terminal_height: int) -> int:
    """Keep the prompt above the terminal edge without overwhelming short terminals."""
    return min(PREFERRED_INPUT_BODY_ROWS, max(4, terminal_height // 4))


def help_table() -> Table:
    table = Table(
        box=None,
        show_edge=False,
        pad_edge=False,
        padding=(0, 2),
    )
    table.add_column("COMMAND", width=40, no_wrap=True, style="magenta")
    table.add_column("DESCRIPTION", width=46, no_wrap=True)
    for command, description in HELP_COMMANDS:
        table.add_row(command, description)
    return table


def parse_model_command(value: str) -> tuple[str, str | None]:
    arguments = value[len("/model") :].strip()
    if not arguments:
        return "show", None
    if arguments.lower() == "thinking":
        raise ValueError("Usage: /model thinking <off|low|high|max>")
    if arguments.lower().startswith("thinking "):
        level = arguments[9:].strip().lower()
        if level not in {"off", "low", "high", "max"}:
            raise ValueError("Usage: /model thinking <off|low|high|max>")
        return "thinking", level
    if any(character.isspace() for character in arguments):
        raise ValueError("Usage: /model <model-name>")
    return "model", arguments


def clear_input_buffer(event) -> None:
    """Clear the current prompt without submitting it."""
    event.current_buffer.reset()


async def wait_for_escape() -> None:
    """Wait for Esc while the Agent owns the terminal input."""
    loop = asyncio.get_running_loop()
    pressed = loop.create_future()
    terminal_input = create_input()

    def input_ready() -> None:
        for key_press in terminal_input.read_keys():
            if key_press.key == Keys.Escape and not pressed.done():
                pressed.set_result(None)

    try:
        with terminal_input.attach(input_ready):
            await pressed
    finally:
        terminal_input.close()


class SlashCommandCompleter(Completer):
    def __init__(
        self,
        session_provider=None,
        active_session_provider=None,
        model_provider=None,
        safety_provider=None,
    ) -> None:
        self.session_provider = session_provider or (lambda: [])
        self.active_session_provider = active_session_provider or (lambda: "")
        self.model_provider = model_provider or (
            lambda: ("deepseek-v4-flash", "enabled", "high")
        )
        self.safety_provider = safety_provider or (lambda: [])

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
        if value.startswith("/model "):
            remainder = value[7:]
            current_model, thinking, effort = self.model_provider()
            current_level = "off" if thinking == "disabled" else effort
            if remainder.lower().startswith("thinking "):
                query = remainder[9:].strip().lower()
                for level in THINKING_LEVELS:
                    if query and not level.startswith(query):
                        continue
                    marker = "● " if level == current_level else ""
                    yield Completion(
                        level,
                        start_position=-len(remainder[9:]),
                        display=f"{marker}{level}",
                        display_meta="current" if level == current_level else "thinking level",
                    )
                return

            query = remainder.strip().lower()
            options = [
                ("thinking ", "Configure thinking: off, low, high, max"),
                *[
                    (
                        model,
                        "current model" if model == current_model else "DeepSeek model",
                    )
                    for model in KNOWN_DEEPSEEK_MODELS
                ],
            ]
            for option, description in options:
                searchable = option.strip().lower()
                if query and not searchable.startswith(query):
                    continue
                marker = "● " if searchable == current_model.lower() else ""
                yield Completion(
                    option,
                    start_position=-len(remainder),
                    display=f"{marker}{option.strip()}",
                    display_meta=description,
                )
            return
        if value.startswith("/safety revoke "):
            query = value[15:].strip().lower()
            for approval in self.safety_provider():
                searchable = (
                    f"{approval.approval_id} {approval.risk} {approval.scope}"
                ).lower()
                if query and query not in searchable:
                    continue
                yield Completion(
                    approval.approval_id,
                    start_position=-len(value[15:]),
                    display=approval.approval_id,
                    display_meta=_truncate_display(
                        f"{approval.risk} · {approval.scope}", 50
                    ),
                )
            return
        if value.startswith("/safety "):
            query = value[8:].strip().lower()
            for option, description in (
                ("revoke ", "Select an approval to revoke"),
                ("reset", "Clear permanent approvals for this workspace"),
            ):
                if query and not option.startswith(query):
                    continue
                yield Completion(
                    option,
                    start_position=-len(value[8:]),
                    display=option.strip(),
                    display_meta=description,
                )
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
        if character in "\r\n":
            if character == "\n":
                rows.append("".join(current))
                current = []
                current_width = 0
            continue
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


def safety_table(
    session_approvals: list[Approval],
    permanent_approvals: list[Approval],
) -> Table:
    table = Table(box=None, show_edge=False, pad_edge=False, padding=(0, 2))
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("LIFETIME", no_wrap=True)
    table.add_column("RISK", max_width=36, overflow="ellipsis", no_wrap=True)
    table.add_column("SCOPE", max_width=48, overflow="ellipsis", no_wrap=True)
    for approval in session_approvals:
        table.add_row(approval.approval_id, "session", approval.risk, approval.scope)
    for approval in permanent_approvals:
        table.add_row(approval.approval_id, "permanent", approval.risk, approval.scope)
    return table


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
    security = SecurityPolicy(workspace.root)
    if security.load_warning:
        console.print(f"[yellow]{security.load_warning}[/yellow]")
    session_approvals: dict[tuple[str, str], Approval] = {}
    denied_commands: set[str] = set()

    def approval_scopes(command: str, cwd: str) -> tuple[str, str]:
        permanent = command_approval_scope(command, cwd, str(workspace.root))
        if permanent is not None:
            return permanent, permanent
        normalized = " ".join(command.split()).casefold()
        digest = hashlib.sha256(f"{cwd.casefold()}\0{normalized}".encode()).hexdigest()[:8]
        return f"exact-command:{digest}", ""

    def print_risk_prompt(command: str, risk, cwd: str, permanent: bool) -> None:
        permanent_option = (
            "[bold magenta]\\[P][/bold magenta] permanent   "
            if permanent
            else "[dim]\\[P] unavailable   [/dim]"
        )
        console.print(
            f"[bold yellow]⚠ Risk:[/bold yellow] {risk.reason}\n"
            f"[dim]cwd:[/dim] {cwd}\n"
            f"[dim]command:[/dim] {command}\n"
            "[bold green]\\[Y][/bold green] once   "
            "[bold cyan]\\[A][/bold cyan] session   "
            f"{permanent_option}"
            "[bold red]\\[N][/bold red] no"
        )

    async def confirm_agent_command(command: str, risk, cwd: str) -> str:
        """Ask before an Agent-issued destructive shell command."""
        session_scope, permanent_scope = approval_scopes(command, cwd)
        key = (risk.reason, session_scope)
        fingerprint = hashlib.sha256(
            f"{cwd.casefold()}\0{' '.join(command.split()).casefold()}".encode()
        ).hexdigest()
        if controller.current_task and fingerprint in denied_commands:
            console.print(
                "[yellow]Command denied: the user already rejected this exact "
                "command during the current task.[/yellow]"
            )
            return "no"
        if key in session_approvals:
            console.print(f"[dim]Risk approved for this session: {session_scope}[/dim]")
            return "always"
        if security.allows(risk.reason, permanent_scope or None):
            console.print(f"[dim]Risk permanently approved: {permanent_scope}[/dim]")
            return "always"
        print_risk_prompt(command, risk, cwd, bool(permanent_scope))
        was_status_active = renderer.status_active
        renderer.stop_status()
        try:
            answer = (await confirmation_session.prompt_async(
                "Approve? [Y/A/P/N] "
            )).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        finally:
            if was_status_active:
                renderer.start_status(plan)
        if answer in {"p", "permanent"}:
            if not permanent_scope:
                console.print(
                    "[yellow]Permanent approval is unavailable because the target "
                    "could not be scoped safely. Command denied.[/yellow]"
                )
                return "no"
            try:
                security.grant(risk.reason, permanent_scope)
            except SecurityPolicyError as exc:
                console.print(
                    f"[yellow]{exc} Allowed once for this command only.[/yellow]"
                )
                return "once"
            return "always"
        if answer in {"a", "always"}:
            approval_id = "s-" + hashlib.sha256(
                f"{risk.reason}\0{session_scope}".encode()
            ).hexdigest()[:8]
            session_approvals[key] = Approval(approval_id, risk.reason, session_scope)
            return "always"
        if answer in {"y", "yes", "once"}:
            return "once"
        if controller.current_task:
            denied_commands.add(fingerprint)
        return "no"

    tools = build_default_registry(
        workspace,
        plan,
        command_timeout_seconds=settings.command_timeout_seconds,
        max_tool_output_chars=settings.max_tool_output_chars,
        confirm_callback=confirm_agent_command,
    )
    renderer = RichRenderer(workspace, console)
    renderer.set_session(active_session.name, active_session.session_id)
    renderer.set_model(
        provider.settings.model,
        provider.settings.thinking,
        provider.settings.reasoning_effort,
    )
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
    controller.restore_session(
        active_session.messages,
        active_session.plan,
        paused_task=active_session.paused_task,
        corrections=active_session.corrections,
    )
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
        active_session.paused_task = controller.current_task if controller.is_paused else ""
        active_session.corrections = controller.corrections if controller.is_paused else []
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
    confirmation_bindings = KeyBindings()
    paused_abort_requested = False

    @confirmation_bindings.add("y", eager=True)
    def approve_once(event) -> None:
        event.app.exit(result="y")

    @confirmation_bindings.add("a", eager=True)
    def approve_session(event) -> None:
        event.app.exit(result="a")

    @confirmation_bindings.add("p", eager=True)
    def approve_permanently(event) -> None:
        event.app.exit(result="p")

    @confirmation_bindings.add("n", eager=True)
    @confirmation_bindings.add("escape", eager=True)
    def deny_command(event) -> None:
        event.app.exit(result="n")

    @key_bindings.add("enter")
    def submit_non_blank(event) -> None:
        if event.current_buffer.text.strip():
            event.current_buffer.validate_and_handle()

    @key_bindings.add(
        "escape",
        filter=Condition(lambda: controller.is_paused),
        eager=True,
    )
    def abandon_paused_task(event) -> None:
        nonlocal paused_abort_requested
        controller.abandon_paused()
        paused_abort_requested = True
        event.app.exit(result="")

    @key_bindings.add(
        "escape",
        "escape",
        filter=Condition(lambda: not controller.is_paused),
    )
    def clear_input(event) -> None:
        clear_input_buffer(event)

    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(history_dir / "history")),
        style=prompt_style,
        completer=SlashCommandCompleter(
            session_provider=session_store.list,
            active_session_provider=lambda: active_session.session_id,
            model_provider=lambda: (
                provider.settings.model,
                provider.settings.thinking,
                provider.settings.reasoning_effort,
            ),
            safety_provider=lambda: [
                *session_approvals.values(),
                *security.approvals(),
            ],
        ),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        # A prompt-toolkit prompt is a live terminal surface. Keeping it in
        # scrollback leaves only its first/top line after the toolbar and
        # right prompt disappear, especially for pasted multi-line input.
        # Erase that surface and print a normal Rich render after submission.
        erase_when_done=True,
        key_bindings=key_bindings,
    )
    confirmation_session: PromptSession[str] = PromptSession(
        style=prompt_style,
        complete_while_typing=False,
        erase_when_done=True,
        key_bindings=confirmation_bindings,
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
    renderer.state_changed(controller.state, plan)

    async def run_agent(operation) -> None:
        renderer.start_status(plan)
        try:
            agent_task = asyncio.create_task(operation)
            escape_task = asyncio.create_task(wait_for_escape())
            done, _ = await asyncio.wait(
                {agent_task, escape_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if escape_task in done and not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except asyncio.CancelledError:
                    pass
                controller.pause()
                renderer.warning(
                    "Task paused by Esc. Add corrections, then use /continue; press Esc again to abandon."
                )
            else:
                escape_task.cancel()
                try:
                    await escape_task
                except asyncio.CancelledError:
                    pass
                await agent_task
        except KeyboardInterrupt:
            controller.pause()
            renderer.warning(
                "Task paused. Add corrections, then use /continue; press Esc again to abandon."
            )
        finally:
            save_current_session()
            renderer.stop_status()

    while True:
        try:
            value = await session.prompt_async(
                input_prompt,
                bottom_toolbar=bottom_toolbar,
                rprompt=FormattedText([("class:input-border", " │")]),
                # PromptSession reserves this height even before completion opens
                # because complete_while_typing is enabled. This keeps the input
                # cursor fixed near the top of a stable input area instead of
                # letting it fall onto the terminal's final row.
                reserve_space_for_menu=input_body_rows(console.height),
            )
            value = value.strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            console.print("[yellow]Input cancelled.[/yellow]")
            continue

        if paused_abort_requested:
            paused_abort_requested = False
            save_current_session()
            console.print(
                "[yellow]Paused task abandoned.[/yellow] Existing file changes were preserved."
            )
            continue

        if not value:
            continue
        # Leave one terminal column unused. Rendering into the last Windows
        # console column can trigger an implicit wrap and visually detach the
        # right/bottom border when the window is resized.
        console.print(submitted_input_text(value, max(24, console.width - 1)))
        if value == "/exit":
            save_current_session()
            break
        if value == "/help":
            console.print(help_table())
            continue
        if value == "/plan":
            renderer.plan_changed(plan)
            continue
        if value == "/diff":
            renderer.show_diff(workspace.diff())
            continue
        if value == "/clear":
            console.clear()
            continue
        if value == "/safety" or value.startswith("/safety "):
            argument = value[7:].strip()
            if argument in {"reset", "clear"}:
                try:
                    security.revoke()
                    console.print("[green]Permanent shell approvals cleared.[/green]")
                except SecurityPolicyError as exc:
                    console.print(f"[red]{exc}[/red]")
            elif argument.startswith("revoke "):
                approval_id = argument[7:].strip()
                session_key = next(
                    (
                        key
                        for key, approval in session_approvals.items()
                        if approval.approval_id == approval_id
                    ),
                    None,
                )
                if session_key is not None:
                    session_approvals.pop(session_key)
                    console.print(
                        f"[green]Revoked session approval: {approval_id}[/green]"
                    )
                    continue
                try:
                    removed = security.revoke(approval_id)
                except SecurityPolicyError as exc:
                    console.print(f"[red]{exc}[/red]")
                    continue
                if removed:
                    console.print(
                        f"[green]Revoked permanent approval: {approval_id}[/green]"
                    )
                else:
                    console.print(f"[yellow]Approval not found: {approval_id}[/yellow]")
            else:
                console.print(f"workspace: {workspace.root}")
                session_items = sorted(
                    session_approvals.values(), key=lambda item: (item.risk, item.scope)
                )
                permanent_items = security.approvals()
                if session_items or permanent_items:
                    console.print(safety_table(session_items, permanent_items))
                else:
                    console.print("[dim]No shell approvals.[/dim]")
                console.print(
                    "[dim]Session approvals expire on exit. Use /safety revoke <id> "
                    "or /safety reset for permanent approvals.[/dim]"
                )
            continue
        if value == "/model" or value.startswith("/model "):
            if controller.state not in {
                AgentState.READY,
                AgentState.PAUSED,
                AgentState.COMPLETED,
                AgentState.FAILED,
                AgentState.WAITING,
            }:
                console.print("[yellow]Pause the current task before changing models.[/yellow]")
                continue
            try:
                action, requested = parse_model_command(value)
                if action == "model":
                    provider.configure(model=requested)
                elif action == "thinking":
                    if requested == "off":
                        provider.configure(thinking="disabled")
                    else:
                        provider.configure(
                            thinking="enabled", reasoning_effort=requested
                        )
            except ValueError as exc:
                console.print(f"[yellow]{exc}[/yellow]")
                continue
            renderer.set_model(
                provider.settings.model,
                provider.settings.thinking,
                provider.settings.reasoning_effort,
            )
            renderer.state_changed(controller.state, plan)
            thinking_label = (
                "off"
                if provider.settings.thinking == "disabled"
                else provider.settings.reasoning_effort
            )
            console.print(
                f"model:    {provider.settings.model}\n"
                f"thinking: {thinking_label}"
            )
            continue
        if value == "/continue":
            if not controller.is_paused:
                console.print("[yellow]There is no paused task.[/yellow]")
                continue
            await run_agent(controller.resume())
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
            renderer.set_session(active_session.name, active_session.session_id)
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
            renderer.set_session(active_session.name, active_session.session_id)
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
            controller.restore_session(
                active_session.messages,
                active_session.plan,
                paused_task=active_session.paused_task,
                corrections=active_session.corrections,
            )
            renderer.restore_errors(active_session.errors)
            renderer.set_session(active_session.name, active_session.session_id)
            session_store.set_active(active_session.session_id)
            renderer.state_changed(controller.state, plan)
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
                    "Use !cd <path>, then run the command separately.[/yellow]"
                )
                continue
            arguments = {"command": command}
            risk = classify_command(command)
            if risk.level == "destructive":
                decision = await confirm_agent_command(command, risk, str(workspace.cwd))
                confirmed = decision in {"once", "always"}
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

        if controller.is_paused:
            controller.add_correction(value)
            save_current_session()
            console.print(
                "[green]Correction recorded.[/green] "
                "Use /continue to resume, or press Esc to abandon the paused task."
            )
            continue

        if active_session.name_source == DEFAULT_NAME_SOURCE:
            active_session.name = derive_session_name(value)
            active_session.name_source = DERIVED_NAME_SOURCE
            renderer.set_session(active_session.name, active_session.session_id)

        denied_commands.clear()
        await run_agent(controller.run(value))

    console.print("[dim]Goodbye.[/dim]")
    return 0


def main() -> int:
    return asyncio.run(run_cli(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
