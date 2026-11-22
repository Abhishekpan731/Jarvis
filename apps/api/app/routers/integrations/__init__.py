"""OAuth connections. Starting one requires a configured provider and an encryption key.

A callback is accepted only for the user who started it, and only once. Tokens
are encrypted before they are written. Disconnecting deletes the ciphertext.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from jarvis_shared.integrations import PROVIDERS, get_provider
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import NotConfigured, ProviderError
from app.db import get_session
from app.integrations import google
from app.integrations.crypto import seal
from app.models import AuditLog, Integration, OAuthState, User
from app.security import error, require_user

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])
_STATE_TTL = timedelta(minutes=10)


def _not_configured() -> HTTPException:
    return HTTPException(
        503,
        error(
            "integration_not_configured",
            "This integration is not configured. Set the provider credential and FERNET_KEY.",
        ),
    )


def _dump(row: Integration) -> dict:
    return {
        "id": str(row.id),
        "provider": row.provider,
        "status": row.status,
        "scopes": list(row.scopes or []),
        "account_label": row.account_label,
        "connected": row.status == "connected" and row.revoked_at is None,
        "connected_at": row.connected_at.isoformat() if row.connected_at else None,
    }


@router.get("")
async def list_integrations(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        (
            await session.execute(
                select(Integration).where(Integration.user_id == user.id).order_by(Integration.provider)
            )
        )
        .scalars()
        .all()
    )
    known = {row.provider: row for row in rows}
    items = []
    for provider_id in PROVIDERS:
        row = known.get(provider_id)
        spec = get_provider(provider_id)
        items.append(
            {
                "provider": provider_id,
                "name": spec.name if spec else provider_id,
                "configured": google.configured(),
                "connection": _dump(row) if row and row.revoked_at is None else None,
            }
        )
    return {"integrations": items}


@router.post("/{provider}/connect")
async def connect(
    provider: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    spec = get_provider(provider)
    if spec is None:
        raise HTTPException(404, error("unknown_provider", "That integration does not exist."))
    if not google.configured():
        raise _not_configured()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    session.add(
        OAuthState(
            state=state,
            user_id=user.id,
            provider=provider,
            code_verifier=verifier,
            expires_at=datetime.now(UTC) + _STATE_TTL,
        )
    )
    await session.commit()
    return {
        "authorize_url": google.authorize_url(spec, state, verifier),
        "provider": provider,
    }


class CallbackIn(BaseModel):
    code: str = ""
    state: str = ""
    error_code: str = Field(default="", max_length=200)


@router.post("/google/callback")
async def callback(
    body: CallbackIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Finish a connection the signed-in user started. A state is not a credential.

    Google redirects the browser to the web app, which posts code and state here
    with the user's access token. The code is not accepted on the query string.
    """
    claimed = await _claim_state(session, body.state, user.id)
    if claimed is None:
        raise HTTPException(400, error("invalid_state", "This connection link is invalid or has expired."))
    if body.error_code or not body.code:
        await session.commit()
        raise HTTPException(400, error("consent_denied", "Google did not grant access. Nothing was stored."))
    try:
        tokens = await google.exchange_code(body.code, claimed.code_verifier)
        ciphertext = seal({k: v for k, v in tokens.items() if v is not None})
    except NotConfigured:
        await session.rollback()
        raise _not_configured() from None
    except ProviderError:
        await session.commit()
        raise HTTPException(
            400, error("token_exchange_failed", "Google refused the code. Start the connection again.")
        ) from None
    await _store(session, claimed, ciphertext)
    session.add(
        AuditLog(
            user_id=user.id,
            actor="user",
            action="integration_connect",
            target_type="integration",
            target_id=claimed.provider,
        )
    )
    await session.commit()
    return {"connected": True, "provider": claimed.provider}


async def _claim_state(session: AsyncSession, state: str, user_id: uuid.UUID) -> OAuthState | None:
    """Mark one unused state consumed. A second request gets no row."""
    if not state:
        return None
    claimed = (
        await session.execute(
            text(
                """
                UPDATE oauth_states
                SET consumed_at = now()
                WHERE state = :state
                  AND user_id = :user_id
                  AND consumed_at IS NULL
                  AND expires_at > now()
                RETURNING state
                """
            ),
            {"state": state, "user_id": user_id},
        )
    ).scalar_one_or_none()
    if claimed is None:
        return None
    return await session.get(OAuthState, claimed)


async def _store(session: AsyncSession, state: OAuthState, ciphertext: str) -> None:
    spec = get_provider(state.provider)
    existing = (
        await session.execute(
            select(Integration).where(
                Integration.user_id == state.user_id, Integration.provider == state.provider
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    scopes = list(spec.scopes) if spec else []
    if existing is None:
        session.add(
            Integration(
                id=uuid.uuid4(),
                user_id=state.user_id,
                provider=state.provider,
                status="connected",
                scopes=scopes,
                token_ciphertext=ciphertext,
                connected_at=now,
                updated_at=now,
            )
        )
        return
    existing.status = "connected"
    existing.scopes = scopes
    existing.token_ciphertext = ciphertext
    existing.connected_at = now
    existing.updated_at = now
    existing.revoked_at = None



@router.delete("/{provider}")
async def disconnect(
    provider: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if get_provider(provider) is None:
        raise HTTPException(404, error("unknown_provider", "That integration does not exist."))
    row = (
        await session.execute(
            select(Integration).where(Integration.user_id == user.id, Integration.provider == provider)
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise HTTPException(404, error("not_connected", "That integration is not connected."))
    now = datetime.now(UTC)
    row.status = "revoked"
    row.token_ciphertext = None
    row.revoked_at = now
    row.updated_at = now
    session.add(
        AuditLog(
            user_id=user.id,
            actor="user",
            action="integration_disconnect",
            target_type="integration",
            target_id=provider,
        )
    )
    await session.commit()
    return {"disconnected": True, "provider": provider}
