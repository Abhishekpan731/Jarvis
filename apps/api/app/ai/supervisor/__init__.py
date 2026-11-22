"""The supervisor. It plans; it does not execute.

A plan is a suggestion until the backend checks it. An agent the registry does
not contain is dropped. An action is plain text describing the step, never a
tool call the model invented. High risk always requires approval, whatever the
model said.
"""

import json
import re

from jarvis_shared.agents import AGENTS, action_allowed, get_agent
from jarvis_shared.integrations import provider_for_agent

from app.ai.base import LanguageModel
from app.ai.prompts import PLANNER
from app.ai.schemas import Plan, PlanStep, Usage
from app.ai.untrusted import quote

_SUPPORTED_AGENT_IDS = tuple(
    agent.id for agent in AGENTS if provider_for_agent(agent.id) is not None
)
_UNSUPPORTED_AGENT_IDS = frozenset(
    agent.id for agent in AGENTS if provider_for_agent(agent.id) is None
)
_HIGH_RISK_ACTION = re.compile(
    r"\b(?:send|sending|sent|email|e-mail|mail|delete|deleting|deleted|remove|removing|removed|"
    r"erase|erasing|destroy|destroying|wipe|wiping|pay|paying|payment|purchase|purchasing|buy|buying|"
    r"order|ordering|charge|charging|refund|refunding|transfer|transferring|wire|wiring|withdraw|"
    r"withdrawing|deploy|deploying|publish|publishing|release|releasing|post|posting|submit|submitting|"
    r"share|sharing|grant|granting|revoke|revoking)\b",
    re.IGNORECASE,
)


def _contains_high_risk_action(text: str) -> bool:
    """Conservatively recognize externally visible or destructive actions."""
    return _HIGH_RISK_ACTION.search(text) is not None


def _requested_unsupported_agents(text: str) -> list[str]:
    """Find named agents that have no executable integration provider."""
    return [
        agent.id
        for agent in AGENTS
        if agent.id in _UNSUPPORTED_AGENT_IDS
        and re.search(rf"\b{re.escape(agent.id)}\b", text, re.IGNORECASE) is not None
    ]


def _unsupported_plan(agent_id: str) -> Plan:
    """Refuse unsupported work without implying that it completed."""
    if agent_id == "research":
        message = (
            "Research is unavailable because no web-search provider is implemented. "
            "I did not create a task."
        )
    else:
        message = f"The {agent_id} capability is not implemented. I did not create a task."
    return Plan(
        intent="unsupported",
        requires_execution=False,
        agents=[],
        risk="low",
        requires_approval=False,
        steps=[],
        direct_answer=message,
    )


def _fallback(message: str) -> Plan:
    """Used when the model is not configured or returns nothing usable.

    A request that names an agent and asks it to do something becomes a plan.
    Everything else is a direct answer, which the caller must not invent.
    """
    unavailable = _requested_unsupported_agents(message)
    if unavailable:
        return _unsupported_plan(unavailable[0])
    text = message.lower()
    high = _contains_high_risk_action(message)
    steps = []
    for agent_id in _SUPPORTED_AGENT_IDS:
        if re.search(rf"\b{re.escape(agent_id)}\b", text) is None:
            continue
        agent = get_agent(agent_id)
        if agent is None:
            continue
        action = f"handle: {message[:120]}"
        if not action_allowed(agent_id, action):
            continue
        steps.append(PlanStep(agent=agent_id, action=action))
    if not steps:
        return Plan(
            intent="conversation",
            requires_execution=False,
            agents=[],
            risk="low",
            requires_approval=False,
            steps=[],
            direct_answer=None,
        )
    return Plan(
        intent="delegated",
        requires_execution=True,
        agents=[step.agent for step in steps],
        risk="high" if high else "low",
        requires_approval=high,
        steps=steps,
    )


def validate_plan(plan: Plan, *, source_text: str | None = None) -> Plan:
    """Drop unsupported agents and force safe refusal or approval behavior."""
    unavailable = _requested_unsupported_agents(source_text or "")
    unavailable.extend(_requested_unsupported_agents(plan.intent))
    planned_unavailable = [
        agent_id
        for agent_id in plan.agents
        if agent_id in _UNSUPPORTED_AGENT_IDS
    ]
    planned_unavailable.extend(
        step.agent for step in plan.steps if step.agent in _UNSUPPORTED_AGENT_IDS
    )
    if unavailable or planned_unavailable:
        return _unsupported_plan((unavailable or planned_unavailable)[0])
    steps = [
        step
        for step in plan.steps
        if step.agent in _SUPPORTED_AGENT_IDS and action_allowed(step.agent, step.action)
    ]
    agents = [agent_id for agent_id in dict.fromkeys(step.agent for step in steps)]
    high = bool(steps) and (
        plan.risk.strip().casefold() != "low"
        or plan.requires_approval
        or _contains_high_risk_action(plan.intent)
        or _contains_high_risk_action(source_text or "")
        or any(_contains_high_risk_action(step.action) for step in steps)
    )
    return plan.model_copy(
        update={
            "steps": steps,
            "agents": agents,
            "requires_execution": bool(steps),
            "risk": "high" if high else "low",
            "requires_approval": high,
        }
    )


async def plan_request(model: LanguageModel | None, message: str) -> tuple[Plan, Usage | None]:
    """Return the validated plan and the token count of the model call.

    The count is None when no model ran. The message is not returned.
    """
    if model is None:
        return validate_plan(_fallback(message), source_text=message), None
    messages = [
        {"role": "system", "content": PLANNER},
        {"role": "system", "content": "Available agents: " + ", ".join(_SUPPORTED_AGENT_IDS)},
        {"role": "user", "content": quote("user request", message, limit=20_000)},
    ]
    completion = await model.complete(messages, schema={"type": "object"})
    try:
        parsed = Plan.model_validate(json.loads(completion.text))
    except (json.JSONDecodeError, ValueError):
        return validate_plan(_fallback(message), source_text=message), completion.usage
    return validate_plan(parsed, source_text=message), completion.usage
