"""Run one integration step from a stored token. Never invents a result.

The worker calls this without holding a database session. This module opens
its own, reads only this user's ciphertext, and makes one read-only call.
A missing connection or a missing credential fails the step.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.ai.base import NotConfigured, ProviderError
from app.db import session_scope
from app.integrations import google
from app.integrations.crypto import open_tokens, seal
from app.models import Integration, TaskStep
from jarvis_shared.integrations import provider_for_agent


class IntegrationStepError(Exception):
    """A step that cannot honestly complete. The message is a stable error code."""


async def run_step(user_id: uuid.UUID, step: TaskStep) -> str:
    provider = provider_for_agent(step.agent_id)
    if provider is None:
        return f"{step.agent_id}: completed"
    if not google.configured():
        raise IntegrationStepError("integration_not_configured")
    tokens = await _load(user_id, provider)
    if tokens is None or not tokens.get("access_token"):
        raise IntegrationStepError("integration_required")
    try:
        return await _read(user_id, provider, tokens)
    except NotConfigured as exc:
        raise IntegrationStepError("integration_not_configured") from exc
    except ProviderError as exc:
        code = str(exc)
        if code == "token_rejected":
            raise IntegrationStepError("integration_token_rejected") from exc
        raise IntegrationStepError("integration_read_failed") from exc


async def _load(user_id: uuid.UUID, provider: str) -> dict | None:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Integration).where(
                    Integration.user_id == user_id,
                    Integration.provider == provider,
                    Integration.status == "connected",
                    Integration.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None or not row.token_ciphertext:
            return None
        try:
            return open_tokens(row.token_ciphertext)
        except NotConfigured as exc:
            raise IntegrationStepError("integration_not_configured") from exc


async def _read(user_id: uuid.UUID, provider: str, tokens: dict) -> str:
    try:
        return await google.read_summary(provider, tokens["access_token"])
    except ProviderError as exc:
        if str(exc) != "token_rejected" or not tokens.get("refresh_token"):
            raise
    refreshed = await google.refresh(tokens["refresh_token"])
    merged = {**tokens, **{k: v for k, v in refreshed.items() if v is not None}}
    if not merged.get("refresh_token"):
        merged["refresh_token"] = tokens.get("refresh_token")
    await _save(user_id, provider, merged)
    return await google.read_summary(provider, merged["access_token"])


async def _save(user_id: uuid.UUID, provider: str, tokens: dict) -> None:
    ciphertext = seal({k: v for k, v in tokens.items() if v is not None})
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Integration).where(
                    Integration.user_id == user_id, Integration.provider == provider
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        row.token_ciphertext = ciphertext
        await session.commit()
