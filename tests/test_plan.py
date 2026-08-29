from __future__ import annotations

from zcode.core.plan import PlanManager, PlanStatus


def test_plan_create_and_update():
    plan = PlanManager()
    plan.create(["Inspect", "Fix", "Verify"])

    assert plan.steps[0].status == PlanStatus.IN_PROGRESS
    plan.update(1, "completed")
    plan.update(2, "in_progress")

    assert plan.completed_count == 1
    assert "✓ 1. Inspect" in plan.render_text()
    assert "▶ 2. Fix" in plan.render_text()
