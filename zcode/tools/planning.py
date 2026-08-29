from __future__ import annotations

from typing import Any

from zcode.core.plan import PlanManager
from zcode.core.types import ToolDefinition, ToolResult
from zcode.tools.base import Tool


class CreatePlanTool(Tool):
    activity = "planning"

    def __init__(self, plan: PlanManager) -> None:
        self.plan = plan
        self.definition = ToolDefinition(
            name="create_plan",
            description=(
                "Create a short execution plan for a non-trivial task after enough read-only investigation. "
                "Do not create plans for trivial single-step edits."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 12,
                    }
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        steps = arguments.get("steps")
        if not isinstance(steps, list) or not all(isinstance(item, str) for item in steps):
            raise ValueError("'steps' must be an array of strings")
        self.plan.create(steps)
        return ToolResult(True, self.plan.render_text(), metadata={"plan_changed": True})

class UpdatePlanTool(Tool):
    activity = "planning"

    def __init__(self, plan: PlanManager) -> None:
        self.plan = plan
        self.definition = ToolDefinition(
            name="update_plan",
            description="Update one or more plan steps when progress or new evidence changes the plan.",
            parameters={
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step_id": {"type": "integer", "minimum": 1},
                                "status": {
                                    "type": "string",
                                    "enum": [
                                        "pending",
                                        "in_progress",
                                        "completed",
                                        "blocked",
                                        "skipped",
                                        "failed",
                                    ],
                                },
                                "description": {"type": "string"},
                            },
                            "required": ["step_id", "status"],
                            "additionalProperties": False,
                        },
                        "minItems": 1,
                    }
                },
                "required": ["updates"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        updates = arguments.get("updates")
        if not isinstance(updates, list):
            raise ValueError("'updates' must be an array")
        for update in updates:
            if not isinstance(update, dict):
                raise ValueError("Each update must be an object")
            self.plan.update(
                int(update.get("step_id")),
                str(update.get("status")),
                update.get("description"),
            )
        return ToolResult(True, self.plan.render_text(), metadata={"plan_changed": True})
