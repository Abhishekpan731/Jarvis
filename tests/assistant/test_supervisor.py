"""The supervisor's plan is validated before anything runs."""

import pytest

from app.ai.schemas import Plan, PlanStep
from app.ai.supervisor import plan_request, validate_plan


def test_unknown_agents_are_removed():
    plan = Plan(
        intent="mischief",
        requires_execution=True,
        agents=["calendar", "root"],
        risk="low",
        requires_approval=False,
        steps=[
            PlanStep(agent="calendar", action="read today's events"),
            PlanStep(agent="root", action="read /etc/shadow"),
        ],
    )
    checked = validate_plan(plan)
    assert checked.agents == ["calendar"]
    assert [step.action for step in checked.steps] == ["read today's events"]


def test_high_risk_actions_require_approval():
    plan = Plan(
        intent="send",
        requires_execution=True,
        agents=["email"],
        risk="low",
        requires_approval=False,
        steps=[PlanStep(agent="email", action="draft_reply and send it to the client")],
    )
    checked = validate_plan(plan)
    assert checked.risk == "high"
    assert checked.requires_approval is True


@pytest.mark.asyncio
async def test_without_a_provider_a_plain_question_creates_no_task():
    plan, usage = await plan_request(None, "What is the capital of France?")
    assert usage is None
    assert plan.requires_execution is False
    assert plan.steps == []


@pytest.mark.asyncio
async def test_without_a_provider_a_named_agent_becomes_a_plan():
    plan, usage = await plan_request(None, "ask the research agent to compare these two libraries")
    assert usage is None
    assert plan.requires_execution is True
    assert plan.agents == ["research"]
    assert plan.requires_approval is False
