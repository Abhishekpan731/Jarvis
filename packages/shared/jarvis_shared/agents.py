"""Agent registry.

An agent is data: an identity, a description, the capabilities it offers, and
the permissions those capabilities require. The orchestrator dispatches work
from this registry; it does not grow a branch per agent.

Permissions are declarative in Phase 1. Nothing grants them at runtime, and no
agent can exceed the ones listed here.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class Risk(StrEnum):
    """How much confirmation an action needs before it runs."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    description: str
    capabilities: tuple[str, ...]
    permissions: tuple[str, ...]
    timeout_seconds: int = 120
    max_retries: int = 2
    risk_ceiling: Risk = Risk.LOW


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        id="personal",
        name="Personal Assistant",
        description="Daily briefings, planning, reminders, and task tracking.",
        capabilities=("briefing", "planning", "reminders", "task_tracking"),
        permissions=("memory:read", "tasks:read", "calendar:read"),
    ),
    AgentSpec(
        id="email",
        name="Email",
        description="Reads, classifies, and summarizes mail, and drafts replies.",
        capabilities=("read", "classify", "summarize", "draft_reply"),
        permissions=("email:read", "email:draft"),
        # Sending is a separate, high-risk capability that is not granted here.
        risk_ceiling=Risk.MEDIUM,
    ),
    AgentSpec(
        id="calendar",
        name="Calendar",
        description="Reads schedules, finds conflicts, and manages events.",
        capabilities=("read", "find_conflicts", "create_event", "reschedule", "cancel"),
        permissions=("calendar:read", "calendar:write"),
        risk_ceiling=Risk.MEDIUM,
    ),
    AgentSpec(
        id="research",
        name="Research",
        description="Researches a question and separates verified facts from assumptions.",
        capabilities=("web_research", "compare", "cite"),
        permissions=("web:read",),
    ),
    AgentSpec(
        id="developer",
        name="Developer",
        description="Reads repositories, explains failures, and prepares code changes.",
        capabilities=("read_repo", "explain_error", "prepare_change"),
        permissions=("repo:read", "repo:write"),
        risk_ceiling=Risk.MEDIUM,
    ),
    AgentSpec(
        id="document",
        name="Document",
        description="Searches, summarizes, and drafts documents in connected storage.",
        capabilities=("search", "summarize", "extract", "draft"),
        permissions=("documents:read", "documents:write"),
        risk_ceiling=Risk.MEDIUM,
    ),
    AgentSpec(
        id="finance",
        name="Finance",
        description="Analyzes budgets and expenses. Never moves money.",
        capabilities=("analyze_budget", "categorize", "calculate", "report"),
        permissions=("finance:read",),
    ),
    AgentSpec(
        id="automation",
        name="Automation",
        description="Runs scheduled and conditional workflows and reports their status.",
        capabilities=("schedule", "monitor", "notify", "retry"),
        permissions=("automations:read", "automations:write", "notifications:write"),
        risk_ceiling=Risk.MEDIUM,
    ),
    AgentSpec(
        id="briefing",
        name="Briefing",
        description="Assembles the daily briefing from data the account actually has.",
        capabilities=("compile_briefing", "prioritize"),
        permissions=("memory:read", "tasks:read"),
    ),
    AgentSpec(
        id="notes",
        name="Notes",
        description="Captures and retrieves notes the user explicitly asked to keep.",
        capabilities=("capture", "retrieve"),
        permissions=("notes:read", "notes:write"),
        risk_ceiling=Risk.MEDIUM,
    ),
    AgentSpec(
        id="contacts",
        name="Contacts",
        description="Looks up people and their preferred contact details.",
        capabilities=("lookup", "summarize_history"),
        permissions=("contacts:read",),
    ),
    AgentSpec(
        id="travel",
        name="Travel",
        description="Compares itineraries and drafts travel plans. Books nothing.",
        capabilities=("compare_itinerary", "draft_plan"),
        permissions=("travel:read",),
    ),
    AgentSpec(
        id="news",
        name="News",
        description="Collects headlines on topics the user follows and cites sources.",
        capabilities=("collect_headlines", "cite"),
        permissions=("web:read",),
    ),
    AgentSpec(
        id="health",
        name="Health",
        description="Summarizes habits and reminders. Gives no medical advice.",
        capabilities=("summarize_habits", "remind"),
        permissions=("health:read",),
    ),
    AgentSpec(
        id="shopping",
        name="Shopping",
        description="Compares products and tracks a list. Purchases nothing.",
        capabilities=("compare_products", "track_list"),
        permissions=("shopping:read",),
    ),
    AgentSpec(
        id="learning",
        name="Learning",
        description="Tracks study topics and suggests the next thing to review.",
        capabilities=("track_topic", "suggest_review"),
        permissions=("learning:read",),
    ),
    AgentSpec(
        id="home",
        name="Home",
        description="Tracks household tasks and recurring chores.",
        capabilities=("track_chore", "schedule_recurrence"),
        permissions=("home:read", "home:write"),
        risk_ceiling=Risk.MEDIUM,
    ),
    AgentSpec(
        id="wellness",
        name="Wellness",
        description="Plans breaks and wind-down routines around the day's schedule.",
        capabilities=("plan_break", "wind_down"),
        permissions=("calendar:read",),
    ),
)

_BY_ID = {agent.id: agent for agent in AGENTS}


def get_agent(agent_id: str) -> AgentSpec | None:
    return _BY_ID.get(agent_id)


def action_allowed(agent_id: str, action: str) -> bool:
    """True only for a registered agent whose action does not name a foreign permission.

    A capability word is not required. The planner writes ordinary sentences, and
    the demo steps are ordinary sentences. What retrieved text cannot do is name
    another agent's permission or invent an agent.
    """
    agent = get_agent(agent_id)
    if agent is None or not action.strip():
        return False
    text = action.lower()
    own = {item.lower() for item in agent.permissions}
    foreign = {
        permission.lower()
        for other in AGENTS
        if other.id != agent.id
        for permission in other.permissions
        if permission.lower() not in own
    }
    return not any(re.search(rf"\b{re.escape(permission)}\b", text) for permission in foreign)
