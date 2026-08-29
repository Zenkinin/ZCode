from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class PlanStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(slots=True)
class PlanStep:
    id: int
    description: str
    status: PlanStatus = PlanStatus.PENDING


@dataclass(slots=True)
class PlanManager:
    steps: list[PlanStep] = field(default_factory=list)

    def clear(self) -> None:
        self.steps.clear()

    def create(self, descriptions: list[str]) -> None:
        cleaned = [item.strip() for item in descriptions if item.strip()]
        if not cleaned:
            raise ValueError("Plan requires at least one non-empty step")
        if len(cleaned) > 12:
            raise ValueError("Plan cannot contain more than 12 steps")
        self.steps = [PlanStep(index, text) for index, text in enumerate(cleaned, 1)]
        self.steps[0].status = PlanStatus.IN_PROGRESS

    def update(self, step_id: int, status: str, description: str | None = None) -> None:
        step = next((item for item in self.steps if item.id == step_id), None)
        if step is None:
            raise ValueError(f"Unknown plan step: {step_id}")
        try:
            step.status = PlanStatus(status)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in PlanStatus)
            raise ValueError(f"Invalid status '{status}'. Allowed: {allowed}") from exc
        if description is not None:
            if not description.strip():
                raise ValueError("Plan step description cannot be empty")
            step.description = description.strip()

    @property
    def completed_count(self) -> int:
        return sum(step.status in {PlanStatus.COMPLETED, PlanStatus.SKIPPED} for step in self.steps)

    @property
    def total_count(self) -> int:
        return len(self.steps)

    def render_text(self) -> str:
        symbols = {
            PlanStatus.PENDING: "○",
            PlanStatus.IN_PROGRESS: "▶",
            PlanStatus.COMPLETED: "✓",
            PlanStatus.BLOCKED: "!",
            PlanStatus.SKIPPED: "~",
            PlanStatus.FAILED: "✕",
        }
        if not self.steps:
            return "(no active plan)"
        return "\n".join(
            f"{symbols[step.status]} {step.id}. {step.description} [{step.status.value}]"
            for step in self.steps
        )
