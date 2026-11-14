"""The supervisor. It plans; it does not execute.

A plan is a suggestion until the backend checks it. An agent the registry does
not contain is dropped. An action is plain text describing the step, never a
tool call the model invented. High risk always requires approval, whatever the
model said.
"""

import json
import re

from jarvis_shared.agents import AGENTS, get_agent

from app.ai.base import LanguageModel
from app.ai.prompts import PLANNER
from app.ai.schemas import Plan, PlanStep

_AGENT_IDS = tuple(agent.id for agent in AGENTS)
_HIGH_RISK_WORDS = ("send", "delete", "pay", "purchase", "deploy", "publish", "transfer")


def _fallback(message: str) -> Plan:
    """Used when the model is not configured or returns nothing usable.

    A request that names an agent and asks it to do something becomes a plan.
    Everything else is a direct answer, which the caller must not invent.
    """
    text = message.lower()
    chosen = [agent_id for agent_id in _AGENT_IDS if re.search(rf"\b{re.escape(agent_id)}\b", text)]
    high = any(word in text for word in _HIGH_RISK_WORDS)
    if not chosen:
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
        agents=chosen,
        risk="high" if high else "low",
        requires_approval=high,
        steps=[PlanStep(agent=agent_id, action=f"handle: {message[:120]}") for agent_id in chosen],
    )


def validate_plan(plan: Plan) -> Plan:
    """Drop anything the registry does not allow, and force the approval gate."""
    steps = [step for step in plan.steps if get_agent(step.agent) is not None]
    agents = [agent_id for agent_id in dict.fromkeys(step.agent for step in steps)]
    high = plan.risk == "high" or any(
        word in step.action.lower() for step in steps for word in _HIGH_RISK_WORDS
    )
    return plan.model_copy(
        update={
            "steps": steps,
            "agents": agents,
            "requires_execution": bool(steps),
            "risk": "high" if high else plan.risk,
            "requires_approval": high or plan.requires_approval,
        }
    )


async def plan_request(model: LanguageModel | None, message: str) -> Plan:
    if model is None:
        return validate_plan(_fallback(message))
    messages = [
        {"role": "system", "content": PLANNER},
        {"role": "system", "content": "Available agents: " + ", ".join(_AGENT_IDS)},
        {"role": "user", "content": message},
    ]
    completion = await model.complete(messages, schema={"type": "object"})
    try:
        parsed = Plan.model_validate(json.loads(completion.text))
    except (json.JSONDecodeError, ValueError):
        return validate_plan(_fallback(message))
    return validate_plan(parsed)
