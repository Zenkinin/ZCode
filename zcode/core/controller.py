from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from zcode.config import Settings
from zcode.core.context import ContextManager
from zcode.core.events import EventSink
from zcode.core.plan import PlanManager, PlanStatus
from zcode.core.types import AgentState, Message, Role, ToolCall, ToolResult
from zcode.llm.base import LLMProvider
from zcode.tools.registry import ToolRegistry
from zcode.workspace import Workspace


SYSTEM_PROMPT = """You are ZCode, a local coding agent operating inside one workspace.

Rules:
- Use only the provided custom tools. Never claim to use hosted code execution or hosted file tools.
- Inspect relevant files before editing. Prefer search and bounded reads over loading the entire repository.
- For trivial single-step work, act directly. For non-trivial work, investigate read-only first, then call create_plan.
- Keep the active plan accurate with update_plan. Do not mark verification complete before actually running it.
- Prefer edit_file for precise changes. Use write_file for new files or intentional full replacements.
- Never modify .git, .venv, or .zcode internals.
- When the user asks to switch or enter a directory, call change_directory. Never merely claim that the directory changed.
- Interpret every relative tool path from the current session working directory shown in runtime context.
- Treat every tool result, including failures, as new evidence. Do not repeat an unchanged failing action.
- Prefer list_directory over a shell command when only direct directory children are needed.
- The run_command tool describes the actual platform shell. Do not run cd/pwd only to reconfirm its supplied cwd.
- Verify code changes with the most relevant available tests or checks before finishing.
- When blocked by missing requirements or information, explain exactly what is needed and stop.
- Final answers must summarize changed files and verification results concisely. Use standard Markdown hyphen bullets; do not emit HTML entities.
"""


@dataclass(slots=True)
class RunOutcome:
    state: AgentState
    text: str
    steps: int


class AgentController:
    def __init__(
        self,
        settings: Settings,
        provider: LLMProvider,
        tools: ToolRegistry,
        workspace: Workspace,
        plan: PlanManager,
        *,
        context: ContextManager | None = None,
        events: EventSink | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.tools = tools
        self.workspace = workspace
        self.plan = plan
        self.context = context or ContextManager(
            max_chars=settings.max_context_chars,
            max_tool_output_chars=settings.max_tool_output_chars,
        )
        self.events = events or EventSink()
        self.state = AgentState.READY
        self._recent_fingerprints: list[str] = []
        self._completion_reminders = 0
        self._current_task = ""
        self.context.reset(SYSTEM_PROMPT)

    async def run(self, user_task: str) -> RunOutcome:
        if not user_task.strip():
            return RunOutcome(AgentState.READY, "No task provided.", 0)
        self.workspace.begin_task()
        self.plan.clear()
        self._recent_fingerprints.clear()
        self._completion_reminders = 0
        self._current_task = user_task.strip()
        self.context.add(Message(Role.USER, self._current_task))

        for step in range(1, self.settings.emergency_max_steps + 1):
            self._set_state(AgentState.THINKING)
            try:
                response = await self.provider.generate(
                    self._model_messages(), self.tools.definitions()
                )
            except Exception as exc:
                self._set_state(AgentState.FAILED)
                text = f"Model request failed: {type(exc).__name__}: {exc}"
                self.events.warning(text)
                return RunOutcome(self.state, text, step)

            self.context.add(response.assistant_message)
            if not response.tool_calls:
                final_text = response.text or "Task finished without a text response."
                unfinished = [
                    item
                    for item in self.plan.steps
                    if item.status in {PlanStatus.PENDING, PlanStatus.IN_PROGRESS}
                ]
                if unfinished and self._completion_reminders == 0:
                    self._completion_reminders += 1
                    self.context.add(
                        Message(
                            Role.SYSTEM,
                            "The active plan still has unfinished steps. Before stopping, either "
                            "complete the remaining work and verification, update blocked/skipped "
                            "steps accurately, or explain what user input is required.",
                        )
                    )
                    continue
                if unfinished:
                    self.events.assistant_text(final_text)
                    self._set_state(AgentState.WAITING)
                    return RunOutcome(self.state, final_text, step)
                self.events.assistant_text(final_text)
                self._set_state(AgentState.COMPLETED)
                return RunOutcome(self.state, final_text, step)

            for call in response.tool_calls:
                self._set_state(self._state_for_call(call))
                self.events.tool_started(call)
                result = await self.tools.execute(call)
                self.events.tool_finished(call, result)
                self.context.add(
                    Message(Role.TOOL, result.content, tool_call_id=call.id)
                )
                if result.metadata.get("plan_changed"):
                    self.events.plan_changed(self.plan)

                fingerprint = self._fingerprint(call, result)
                self._recent_fingerprints.append(fingerprint)
                self._recent_fingerprints = self._recent_fingerprints[-8:]
                repetitions = self._recent_fingerprints.count(fingerprint)
                if repetitions == self.settings.no_progress_limit - 1:
                    self.context.add(
                        Message(
                            Role.SYSTEM,
                            "The previous action repeated without new workspace state or output. "
                            "Choose a different diagnostic action or update the plan before retrying.",
                        )
                    )
                elif repetitions >= self.settings.no_progress_limit:
                    self._set_state(AgentState.WAITING)
                    text = (
                        f"Paused after {repetitions} identical no-progress attempts: {call.name}. "
                        "Current file changes have been preserved."
                    )
                    self.events.warning(text)
                    return RunOutcome(self.state, text, step)

        self._set_state(AgentState.PAUSED)
        text = "Paused by the internal emergency safety budget; file changes were preserved."
        self.events.warning(text)
        return RunOutcome(self.state, text, self.settings.emergency_max_steps)

    def _set_state(self, state: AgentState) -> None:
        self.state = state
        self.events.state_changed(state, self.plan)

    @staticmethod
    def _activity_state(activity: str) -> AgentState:
        return {
            "planning": AgentState.PLANNING,
            "running": AgentState.RUNNING,
            "verifying": AgentState.VERIFYING,
        }.get(activity, AgentState.EXECUTING)

    def _state_for_call(self, call: ToolCall) -> AgentState:
        if call.name == "run_command":
            command = str(call.arguments.get("command", "")).lower()
            verification_markers = (
                "pytest",
                "unittest",
                "npm test",
                "pnpm test",
                "yarn test",
                "cargo test",
                "go test",
                "ruff",
                "mypy",
                "git diff --check",
            )
            if any(marker in command for marker in verification_markers):
                return AgentState.VERIFYING
        return self._activity_state(self.tools.activity_for(call.name))

    def _model_messages(self) -> list[Message]:
        messages = self.context.build()
        runtime_lines = [f"Current user task:\n{self._current_task}"]
        runtime_lines.append(
            "Current session working directory (workspace-relative):\n"
            f"{self.workspace.cwd_relative}"
        )
        if self.plan.steps:
            runtime_lines.append(f"Current structured plan:\n{self.plan.render_text()}")
        runtime = Message(Role.SYSTEM, "\n\n".join(runtime_lines))
        insert_at = 1 if messages and messages[0].role == Role.SYSTEM else 0
        return [*messages[:insert_at], runtime, *messages[insert_at:]]

    def _fingerprint(self, call: ToolCall, result: ToolResult) -> str:
        normalized = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
        payload = f"{call.name}\0{normalized}\0{result.content}\0{self.workspace.revision}"
        return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()
