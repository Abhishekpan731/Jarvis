"""Foundation schema: identity, agents, tasks, audit.

Revision ID: 0001
Revises:
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASK_STATUSES = (
    "created",
    "understanding",
    "planning",
    "waiting_for_approval",
    "executing",
    "verifying",
    "completed",
    "failed",
    "cancelled",
    "paused",
    "waiting_for_user",
    "requires_retry",
)

# Seeded from packages/shared/jarvis_shared/agents.py. If these drift, the test
# suite fails rather than the orchestrator dispatching to an unknown agent.
AGENTS = [
    ("personal", "Personal Assistant", "Daily briefings, planning, reminders, and task tracking.", 120, 2, "low", ("briefing", "planning", "reminders", "task_tracking")),
    ("email", "Email", "Reads, classifies, and summarizes mail, and drafts replies.", 120, 2, "medium", ("read", "classify", "summarize", "draft_reply")),
    ("calendar", "Calendar", "Reads schedules, finds conflicts, and manages events.", 120, 2, "medium", ("read", "find_conflicts", "create_event", "reschedule", "cancel")),
    ("research", "Research", "Researches a question and separates verified facts from assumptions.", 180, 2, "low", ("web_research", "compare", "cite")),
    ("developer", "Developer", "Reads repositories, explains failures, and prepares code changes.", 300, 2, "medium", ("read_repo", "explain_error", "prepare_change")),
    ("document", "Document", "Searches, summarizes, and drafts documents in connected storage.", 180, 2, "medium", ("search", "summarize", "extract", "draft")),
    ("finance", "Finance", "Analyzes budgets and expenses. Never moves money.", 120, 2, "low", ("analyze_budget", "categorize", "calculate", "report")),
    ("automation", "Automation", "Runs scheduled and conditional workflows and reports their status.", 120, 2, "medium", ("schedule", "monitor", "notify", "retry")),
    ("briefing", "Briefing", "Assembles the daily briefing from data the account actually has.", 60, 2, "low", ("compile_briefing", "prioritize")),
    ("notes", "Notes", "Captures and retrieves notes the user explicitly asked to keep.", 60, 2, "medium", ("capture", "retrieve")),
    ("contacts", "Contacts", "Looks up people and their preferred contact details.", 60, 2, "low", ("lookup", "summarize_history")),
    ("travel", "Travel", "Compares itineraries and drafts travel plans. Books nothing.", 120, 2, "low", ("compare_itinerary", "draft_plan")),
    ("news", "News", "Collects headlines on topics the user follows and cites sources.", 90, 2, "low", ("collect_headlines", "cite")),
    ("health", "Health", "Summarizes habits and reminders. Gives no medical advice.", 60, 2, "low", ("summarize_habits", "remind")),
    ("shopping", "Shopping", "Compares products and tracks a list. Purchases nothing.", 90, 2, "low", ("compare_products", "track_list")),
    ("learning", "Learning", "Tracks study topics and suggests the next thing to review.", 60, 2, "low", ("track_topic", "suggest_review")),
    ("home", "Home", "Tracks household tasks and recurring chores.", 60, 2, "medium", ("track_chore", "schedule_recurrence")),
    ("wellness", "Wellness", "Plans breaks and wind-down routines around the day's schedule.", 60, 2, "low", ("plan_break", "wind_down")),
]


def upgrade() -> None:
    op.execute("create extension if not exists pgcrypto")
    op.execute("create extension if not exists vector")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text, nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "user_preferences",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("timezone", sa.Text, nullable=False, server_default="UTC"),
        sa.Column("briefing_language", sa.Text, nullable=False, server_default="en"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("briefing_language in ('en', 'hi')", name="preferences_language_valid"),
    )

    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("refresh_token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("replaced_by", postgresql.UUID(as_uuid=True)),
        sa.Column("user_agent", sa.Text),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "agents",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("timeout_seconds", sa.Integer, nullable=False),
        sa.Column("max_retries", sa.Integer, nullable=False),
        sa.Column("risk_ceiling", sa.Text, nullable=False),
    )
    op.create_table(
        "agent_capabilities",
        sa.Column("agent_id", sa.Text, sa.ForeignKey("agents.id"), primary_key=True),
        sa.Column("capability", sa.Text, primary_key=True),
    )
    op.bulk_insert(
        sa.table(
            "agents",
            sa.column("id", sa.Text),
            sa.column("name", sa.Text),
            sa.column("description", sa.Text),
            sa.column("timeout_seconds", sa.Integer),
            sa.column("max_retries", sa.Integer),
            sa.column("risk_ceiling", sa.Text),
        ),
        [
            {"id": a[0], "name": a[1], "description": a[2], "timeout_seconds": a[3], "max_retries": a[4], "risk_ceiling": a[5]}
            for a in AGENTS
        ],
    )
    op.bulk_insert(
        sa.table(
            "agent_capabilities",
            sa.column("agent_id", sa.Text),
            sa.column("capability", sa.Text),
        ),
        [{"agent_id": a[0], "capability": cap} for a in AGENTS for cap in a[6]],
    )

    status_check = "status in (" + ",".join(f"'{s}'" for s in TASK_STATUSES) + ")"
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("instruction", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="created"),
        sa.Column("result", postgresql.JSONB),
        sa.Column("error", sa.Text),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(status_check, name="tasks_status_valid"),
    )
    op.create_index("tasks_user_status_idx", "tasks", ["user_id", "status"])

    op.create_table(
        "task_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False, index=True),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("agent_id", sa.Text, sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("risk", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("task_id", "position"),
    )

    op.create_table(
        "task_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("from_status", sa.Text),
        sa.Column("to_status", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("task_events_task_created_idx", "task_events", ["task_id", "created_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target_type", sa.Text),
        sa.Column("target_id", sa.Text),
        sa.Column("metadata", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # Row-level security is the second barrier. The application sets
    # `app.user_id` on its connection; direct queries without it see nothing.
    for table in ("tasks", "task_steps", "task_events"):
        op.execute(f"alter table {table} enable row level security")
    op.execute(
        "create policy tasks_owner on tasks using (user_id = current_setting('app.user_id', true)::uuid)"
    )


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("task_events")
    op.drop_table("task_steps")
    op.drop_table("tasks")
    op.drop_table("agent_capabilities")
    op.drop_table("agents")
    op.drop_table("sessions")
    op.drop_table("user_preferences")
    op.drop_table("users")
