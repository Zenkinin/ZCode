from __future__ import annotations

import subprocess
import time
import re
from pathlib import Path

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from zcode.core.events import EventSink
from zcode.core.plan import PlanManager, PlanStatus
from zcode.core.types import AgentState, ToolCall, ToolResult
from zcode.workspace import Workspace


STATE_STYLE: dict[AgentState, tuple[str, str]] = {
    AgentState.READY: ("○", "dim"),
    AgentState.THINKING: ("◐", "cyan"),
    AgentState.PLANNING: ("◐", "blue"),
    AgentState.EXECUTING: ("●", "magenta"),
    AgentState.RUNNING: ("▶", "bright_blue"),
    AgentState.VERIFYING: ("◐", "green"),
    AgentState.WAITING: ("?", "yellow"),
    AgentState.PAUSED: ("⏸", "yellow"),
    AgentState.COMPLETED: ("✓", "green"),
    AgentState.FAILED: ("✕", "red"),
}

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
ANIMATED_STATES = {
    AgentState.THINKING,
    AgentState.PLANNING,
    AgentState.EXECUTING,
    AgentState.RUNNING,
    AgentState.VERIFYING,
}


class RichRenderer(EventSink):
    def __init__(self, workspace: Workspace | Path, console: Console | None = None) -> None:
        self.workspace_state = workspace if isinstance(workspace, Workspace) else None
        self.workspace = workspace.root if isinstance(workspace, Workspace) else workspace
        self.console = console or Console()
        self._last_state: AgentState | None = None
        self._last_plan: PlanManager | None = None
        self._git_label = self._git_status()
        self._status_live: Live | None = None
        self.error_records: list[dict[str, object]] = []
        self._session_name = "—"
        self._session_id = ""
        self._model = "—"
        self._thinking = "—"

    def set_session(self, name: str, session_id: str) -> None:
        self._session_name = name
        self._session_id = session_id
        if self._status_live is not None:
            self._status_live.update(self, refresh=True)

    def set_model(self, model: str, thinking: str, reasoning_effort: str) -> None:
        self._model = model
        self._thinking = "off" if thinking == "disabled" else reasoning_effort
        if self._status_live is not None:
            self._status_live.update(self, refresh=True)

    def banner(self, model: str) -> None:
        body = (
            f"[bold]workspace[/bold]  {self.workspace}\n"
            f"[bold]model[/bold]      {model}\n"
            f"[bold]git[/bold]        {self._git_status()}"
        )
        self.console.print(Panel(body, title="[bold blue]ZCode[/bold blue]", expand=False))
        self.console.print("[dim]Type /help for commands.[/dim]")

    def state_changed(self, state: AgentState, plan: PlanManager) -> None:
        self._last_state = state
        self._last_plan = plan
        self._git_label = self._git_status()
        if self._status_live is not None:
            self._status_live.update(self, refresh=True)

    @property
    def current_state(self) -> AgentState:
        return self._last_state or AgentState.READY

    def status_parts(
        self,
        plan: PlanManager | None = None,
        *,
        spinner_frame: int | None = None,
    ) -> tuple[str, str]:
        active_plan = plan or self._last_plan
        state = self.current_state
        symbol = self._state_symbol(state, spinner_frame)
        progress = "—"
        if active_plan is not None and active_plan.total_count:
            progress = f"{active_plan.completed_count}/{active_plan.total_count}"
        primary = f"{symbol} {state.value}"
        cwd = self.workspace_state.cwd_relative if self.workspace_state else "."
        details = (
            f" │ model: {self._model}/{self._thinking} │ session: {self._session_name} │ cwd: {cwd} "
            f"│ git: {self._git_label} │ plan {progress}"
        )
        return primary, details

    @staticmethod
    def _state_symbol(state: AgentState, spinner_frame: int | None = None) -> str:
        if state not in ANIMATED_STATES:
            return STATE_STYLE[state][0]
        frame = spinner_frame
        if frame is None:
            frame = int(time.monotonic() * 10)
        return SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]

    def status_text(self, plan: PlanManager | None = None) -> Text:
        state = self.current_state
        _, style = STATE_STYLE[state]
        primary, details = self.status_parts(plan)
        line = Text()
        line.append(primary, style=style)
        line.append(details, style="dim")
        return line

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        """Rebuild the status on every Live refresh so its spinner advances."""
        yield self.status_text()

    def start_status(self, plan: PlanManager) -> None:
        if self._status_live is not None:
            return
        self._last_plan = plan
        self._status_live = Live(
            self,
            console=self.console,
            refresh_per_second=10,
            transient=True,
        )
        self._status_live.start(refresh=True)

    @property
    def status_active(self) -> bool:
        return self._status_live is not None

    def stop_status(self) -> None:
        if self._status_live is None:
            return
        self._status_live.stop()
        self._status_live = None

    def tool_started(self, call: ToolCall) -> None:
        label = {
            "read_file": "Read",
            "list_directory": "List",
            "search_text": "Search",
            "edit_file": "Edit",
            "write_file": "Write",
            "run_command": "Run",
            "change_directory": "Change directory",
            "create_plan": "Create plan",
            "update_plan": "Update plan",
        }.get(call.name, call.name)
        target = call.arguments.get("path") or call.arguments.get("command") or ""
        line = Text("● " + label, style="bold")
        if target:
            line.append(" " + str(target), style="dim")
        self.console.print(line)

    def tool_finished(self, call: ToolCall, result: ToolResult) -> None:
        if not result.success:
            error_id = self._record_error(call, result)
            exit_code = result.metadata.get("exit_code")
            heading = f"  ✕ {call.name}"
            if exit_code is not None:
                heading += f" · exit_code: {exit_code}"
            heading += f" · error {error_id}"
            self.console.print(Text(heading, style="red"))
            summary = str(self.error_records[-1]["summary"])
            if summary:
                self.console.print(Text("    " + summary, style="red dim"))
            self.console.print(
                Text(f"    Use /error {error_id} for full output", style="dim")
            )
            return
        if call.name in {"edit_file", "write_file"} and result.content.startswith("---"):
            self.console.print(Syntax(result.content, "diff", theme="ansi_dark", word_wrap=True))
        elif call.name == "run_command":
            self.console.print(
                Panel(Text(result.content), border_style="blue", expand=False)
            )
        else:
            self.console.print("[green]  ✓[/green]")
        if result.metadata.get("cwd_changed") and self._status_live is not None:
            self._status_live.update(self, refresh=True)

    def plan_changed(self, plan: PlanManager) -> None:
        symbols = {
            PlanStatus.PENDING: ("○", "dim"),
            PlanStatus.IN_PROGRESS: ("▶", "magenta"),
            PlanStatus.COMPLETED: ("✓", "green"),
            PlanStatus.BLOCKED: ("!", "yellow"),
            PlanStatus.SKIPPED: ("~", "dim"),
            PlanStatus.FAILED: ("✕", "red"),
        }
        text = Text()
        for step in plan.steps:
            symbol, style = symbols[step.status]
            text.append(f"{symbol} {step.description}\n", style=style)
        self.console.print(Panel(text, title="Plan", border_style="blue", expand=False))

    def assistant_text(self, text: str) -> None:
        self.console.print()
        self.console.print(Markdown(text))

    def warning(self, text: str) -> None:
        self.console.print(f"[yellow]⚠ {text}[/yellow]")

    def show_diff(self, diff: str) -> None:
        if not diff:
            self.console.print("[dim]No changes recorded for the current task.[/dim]")
            return
        self.console.print(Syntax(diff, "diff", theme="ansi_dark", word_wrap=True))

    def restore_errors(self, records: list[dict[str, object]]) -> None:
        self.error_records = [dict(record) for record in records]

    def show_error(self, error_id: str | None = None) -> bool:
        record = None
        if error_id:
            record = next(
                (item for item in self.error_records if item.get("id") == error_id),
                None,
            )
        elif self.error_records:
            record = self.error_records[-1]
        if record is None:
            return False
        self.console.print(
            Panel(
                Text(str(record.get("content", ""))),
                title=f"Error {record.get('id', '')}",
                border_style="red",
                expand=False,
            )
        )
        return True

    def show_errors(self) -> None:
        if not self.error_records:
            self.console.print("[dim]No errors recorded in this session.[/dim]")
            return
        table = Table(box=None, show_edge=False, pad_edge=False)
        table.add_column("ID", style="red", no_wrap=True)
        table.add_column("TOOL", style="bold", no_wrap=True)
        table.add_column("SUMMARY", overflow="ellipsis")
        for record in reversed(self.error_records):
            table.add_row(
                str(record.get("id", "")),
                str(record.get("tool", "")),
                str(record.get("summary", "")),
            )
        self.console.print(table)

    def _record_error(self, call: ToolCall, result: ToolResult) -> str:
        next_number = 1
        if self.error_records:
            try:
                next_number = int(str(self.error_records[-1].get("id", "e-000"))[2:]) + 1
            except ValueError:
                next_number = len(self.error_records) + 1
        error_id = f"e-{next_number:03d}"
        content = self._redact_secrets(result.content)
        useful_lines = [line.strip() for line in content.splitlines() if line.strip()]
        summary = " · ".join(useful_lines[:3])
        if len(summary) > 240:
            summary = summary[:239] + "…"
        self.error_records.append(
            {
                "id": error_id,
                "tool": call.name,
                "summary": summary,
                "content": content,
                "exit_code": result.metadata.get("exit_code"),
            }
        )
        return error_id

    @staticmethod
    def _redact_secrets(value: str) -> str:
        return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "sk-[REDACTED]", value)

    def _git_status(self) -> str:
        try:
            branch = subprocess.run(
                ["git", "-C", str(self.workspace), "branch", "--show-current"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            if not branch:
                return "—"
            dirty = subprocess.run(
                ["git", "-C", str(self.workspace), "status", "--porcelain"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            return f"{branch}{'*' if dirty else ''}"
        except (OSError, subprocess.SubprocessError):
            return "—"
