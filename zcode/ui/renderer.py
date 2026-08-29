from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
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


class RichRenderer(EventSink):
    def __init__(self, workspace: Workspace | Path, console: Console | None = None) -> None:
        self.workspace_state = workspace if isinstance(workspace, Workspace) else None
        self.workspace = workspace.root if isinstance(workspace, Workspace) else workspace
        self.console = console or Console()
        self._last_state: AgentState | None = None
        self._last_plan: PlanManager | None = None
        self._git_label = self._git_status()
        self._status_live: Live | None = None

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
            self._status_live.update(self.status_text(plan), refresh=True)

    @property
    def current_state(self) -> AgentState:
        return self._last_state or AgentState.READY

    def status_parts(self, plan: PlanManager | None = None) -> tuple[str, str]:
        active_plan = plan or self._last_plan
        state = self.current_state
        symbol, _ = STATE_STYLE[state]
        progress = "—"
        if active_plan is not None and active_plan.total_count:
            progress = f"{active_plan.completed_count}/{active_plan.total_count}"
        primary = f"{symbol} {state.value}"
        cwd = self.workspace_state.cwd_relative if self.workspace_state else "."
        details = f" │ cwd: {cwd} │ git: {self._git_label} │ plan {progress}"
        return primary, details

    def status_text(self, plan: PlanManager | None = None) -> Text:
        state = self.current_state
        symbol, style = STATE_STYLE[state]
        primary, details = self.status_parts(plan)
        line = Text()
        line.append(primary, style=style)
        line.append(details, style="dim")
        return line

    def start_status(self, plan: PlanManager) -> None:
        if self._status_live is not None:
            return
        self._last_plan = plan
        self._status_live = Live(
            self.status_text(plan),
            console=self.console,
            refresh_per_second=10,
            transient=True,
        )
        self._status_live.start(refresh=True)

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
            self.console.print(Text("  ✕ " + result.content, style="red"))
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
            self._status_live.update(self.status_text(), refresh=True)

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
