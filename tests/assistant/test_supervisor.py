"""The supervisor's plan is validated before anything runs."""

import json

import pytest
from app.ai.schemas import Completion, Plan, PlanStep, Usage
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
async def test_without_a_provider_rejects_unsupported_research():
    plan, usage = await plan_request(None, "research compare the two libraries")
    assert usage is None
    assert plan.requires_execution is False
    assert plan.steps == []
    assert plan.agents == []
    assert plan.direct_answer == (
        "Research is unavailable because no web-search provider is implemented. "
        "I did not create a task."
    )

class LowRiskModel:
    """Return benign-looking plan metadata for adversarial request tests."""

    name = "test"
    model = "test-model"

    async def complete(self, _messages, *, schema=None):
        assert "research" not in _messages[1]["content"]
        assert "email" in _messages[1]["content"]
        assert "calendar" in _messages[1]["content"]
        plan = {
            "intent": "compare two libraries",
            "requires_execution": True,
            "agents": ["email"],
            "risk": "low",
            "requires_approval": False,
            "steps": [{"agent": "email", "action": "summarize the inbox"}],
        }
        return Completion(
            text=json.dumps(plan),
            model=self.model,
            provider=self.name,
            usage=Usage(),
        )


@pytest.mark.asyncio
async def test_model_cannot_downgrade_a_risky_user_request():
    plan, _ = await plan_request(LowRiskModel(), "ask email to wire the funds to a vendor")
    assert plan.risk == "high"
    assert plan.requires_approval is True


def test_non_low_model_risk_can_only_escalate_approval():
    plan = Plan(
        intent="compare two libraries",
        requires_execution=True,
        agents=["email"],
        risk="medium",
        requires_approval=False,
        steps=[PlanStep(agent="email", action="summarize the inbox")],
    )
    checked = validate_plan(plan)
    assert checked.risk == "high"
    assert checked.requires_approval is True


def test_validate_plan_rejects_research_without_a_search_handler():
    plan = Plan(
        intent="research comparison",
        requires_execution=True,
        agents=["research"],
        risk="low",
        requires_approval=False,
        steps=[PlanStep(agent="research", action="compare two libraries")],
        direct_answer="I compared the libraries.",
    )

    checked = validate_plan(plan, source_text="research compare the libraries")

    assert checked.requires_execution is False
    assert checked.steps == []
    assert checked.agents == []
    assert checked.direct_answer == (
        "Research is unavailable because no web-search provider is implemented. "
        "I did not create a task."
    )


@pytest.mark.asyncio
async def test_model_cannot_claim_to_have_researched_unsupported_work():
    plan, _ = await plan_request(LowRiskModel(), "research compare the two libraries")

    assert plan.requires_execution is False
    assert plan.steps == []
    assert plan.direct_answer == (
        "Research is unavailable because no web-search provider is implemented. "
        "I did not create a task."
    )
