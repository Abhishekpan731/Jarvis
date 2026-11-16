"""Which planned agents need a connected account before work is queued."""

import uuid

from jarvis_shared.integrations import provider_for_agent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Integration


async def missing_connection(
    session: AsyncSession, user_id: uuid.UUID, agents: list[str]
) -> str | None:
    """The first provider the plan needs that this user has not connected."""
    needed = []
    for agent_id in agents:
        provider = provider_for_agent(agent_id)
        if provider and provider not in needed:
            needed.append(provider)
    if not needed:
        return None
    rows = (
        (
            await session.execute(
                select(Integration.provider).where(
                    Integration.user_id == user_id,
                    Integration.provider.in_(needed),
                    Integration.status == "connected",
                    Integration.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    connected = set(rows)
    for provider in needed:
        if provider not in connected:
            return provider
    return None
