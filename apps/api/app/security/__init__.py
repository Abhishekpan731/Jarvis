"""Passwords, tokens, and the authenticated user.

Access tokens are JWTs and live for minutes. Refresh tokens are random, stored
only as a SHA-256 hash, and travel only in an HttpOnly cookie. A refresh token
presented again after it has been rotated revokes every session in its family,
because that is what a stolen token looks like.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session, set_tenant_guc
from app.models import Session, User

# Explicit Argon2id cost parameters (never rely on library defaults, which
# can change between releases and silently shift the application's security
# posture). These exceed every equal-defense configuration listed in the
# OWASP Password Storage Cheat Sheet (m=19 MiB/t=2/p=1 through m=46 MiB/t=1/p=1).
# Parallelism is pinned to 1: OWASP notes raising it does not reliably buy
# extra resistance to cracking, and a lower value keeps the cost of hashing
# one password predictable on a single request thread under concurrent load.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
_bearer = HTTPBearer(auto_error=False)
REFRESH_COOKIE = "jarvis_refresh"


def error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def access_token_for(user_id: uuid.UUID, session_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "sid": str(session_id),
            "iat": now,
            "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def refresh_cookie(token: str) -> dict:
    return {
        "key": REFRESH_COOKIE,
        "value": token,
        "max_age": settings.refresh_token_ttl_seconds,
        "httponly": True,
        "secure": settings.app_env == "production",
        "samesite": "lax",
        "path": "/api/v1/auth",
    }


async def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Operator gate. A user access token does not open this.

    The token is compared in constant time. An empty configuration refuses
    every request, including one that sends a blank bearer value.
    """
    configured = settings.admin_token
    presented = credentials.credentials if credentials is not None else ""
    if not configured or not secrets.compare_digest(presented, configured):
        raise HTTPException(401, error("unauthenticated", "Operator sign-in required."))


async def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    if credentials is None:
        raise HTTPException(401, error("unauthenticated", "Sign in to continue."))
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
        user_id = uuid.UUID(payload["sub"])
        session_id = uuid.UUID(payload["sid"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(401, error("unauthenticated", "Sign in to continue."))

    row = await session.get(Session, session_id)
    if row is None or row.revoked_at is not None or row.user_id != user_id:
        raise HTTPException(401, error("session_revoked", "Sign in to continue."))
    user = await session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(401, error("unauthenticated", "Sign in to continue."))
    await set_tenant_guc(session, user.id)
    return user


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def open_session(
    session: AsyncSession, user: User, user_agent: str | None
) -> tuple[Session, str]:
    """Create a session and return it with the raw refresh token."""
    raw = new_refresh_token()
    row = Session(
        id=uuid.uuid4(),
        user_id=user.id,
        refresh_token_hash=hash_token(raw),
        family_id=uuid.uuid4(),
        user_agent=user_agent,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
    )
    session.add(row)
    await session.flush()
    return row, raw


async def rotate_session(session: AsyncSession, raw_token: str) -> tuple[User, Session, str]:
    """Exchange a refresh token for a new one, or revoke the family on reuse.

    The row is locked so two concurrent refreshes cannot both succeed. The
    loser sees the token already replaced and revokes the family.
    """
    row = (
        await session.execute(
            select(Session)
            .where(Session.refresh_token_hash == hash_token(raw_token))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.expires_at < datetime.now(UTC):
        raise HTTPException(401, error("unauthenticated", "Sign in to continue."))

    if row.revoked_at is not None or row.replaced_by is not None:
        await _revoke_family(session, row.family_id)
        await session.commit()
        raise HTTPException(401, error("session_revoked", "Sign in to continue."))

    user = await session.get(User, row.user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(401, error("unauthenticated", "Sign in to continue."))

    replacement, raw = await open_session(session, user, row.user_agent)
    replacement.family_id = row.family_id
    row.replaced_by = replacement.id
    row.revoked_at = datetime.now(UTC)
    return user, replacement, raw


async def revoke_session(session: AsyncSession, session_id: uuid.UUID) -> None:
    row = await session.get(Session, session_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)


async def revoke_all_sessions(session: AsyncSession, user_id: uuid.UUID) -> None:
    """End every session for an account. Used on password change and deletion."""
    rows = (await session.execute(select(Session).where(Session.user_id == user_id))).scalars()
    now = datetime.now(UTC)
    for row in rows:
        row.revoked_at = row.revoked_at or now


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    rows = (await session.execute(select(Session).where(Session.family_id == family_id))).scalars()
    now = datetime.now(UTC)
    for row in rows:
        row.revoked_at = row.revoked_at or now
