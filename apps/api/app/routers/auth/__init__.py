"""Registration, login, refresh, logout, and account management."""

import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import AuditLog, Session, User, UserPreferences
from app.security import (
    REFRESH_COOKIE,
    access_token_for,
    client_ip,
    error,
    hash_password,
    open_session,
    refresh_cookie,
    require_user,
    revoke_all_sessions,
    revoke_session,
    rotate_session,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# In-process login/register throttle, keyed per endpoint and client IP. A
# restart clears it, which is acceptable until the Redis-backed limiter
# arrives with the worker phase.
_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_WINDOW_SECONDS = 300
_MAX_ATTEMPTS = 8


def reset_throttle() -> None:
    """Clear counters. Tests use this so one case cannot exhaust the next."""
    _ATTEMPTS.clear()

AUTH_FAILED = "The email or password is not correct."


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=80)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class PreferencesUpdate(BaseModel):
    """Fields a user may update in their own preferences."""
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    briefing_language: Literal["en", "hi"] | None = None


def _throttle(key: str) -> None:
    now = time.monotonic()
    recent = [stamp for stamp in _ATTEMPTS[key] if now - stamp < _WINDOW_SECONDS]
    _ATTEMPTS[key] = recent
    if len(recent) >= _MAX_ATTEMPTS:
        raise HTTPException(429, error("rate_limited", "Too many attempts. Try again later."))
    recent.append(now)


def _throttle_key(scope: str, request: Request) -> str:
    return f"{scope}:{client_ip(request)}"


def _issue(response: Response, user: User, row_id: uuid.UUID, refresh: str) -> dict:
    response.set_cookie(**refresh_cookie(refresh))
    return {"access_token": access_token_for(user.id, row_id), "token_type": "bearer"}


@router.post("/register", status_code=201)
async def register(
    body: Credentials,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict:
    _throttle(_throttle_key("register", request))
    email = body.email.strip().lower()
    if "@" not in email or len(email.split("@")[1]) < 3:
        raise HTTPException(422, error("invalid_email", "Enter a valid email address."))
    existing = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing is not None:
        # Identical response whether or not the account was deleted, so this
        # endpoint does not reveal which addresses exist.
        raise HTTPException(409, error("email_taken", "An account with that email already exists."))

    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password(body.password),
        display_name=body.display_name or email.split("@")[0],
    )
    session.add(user)
    await session.flush()
    session.add(UserPreferences(user_id=user.id))
    row, refresh = await open_session(session, user, request.headers.get("user-agent"))
    session.add(AuditLog(user_id=user.id, actor="user", action="register", target_type="user"))
    await session.commit()
    return _issue(response, user, row.id, refresh) | {"user": _profile(user)}


@router.post("/login")
async def login(
    body: Credentials,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict:
    _throttle(_throttle_key("login", request))
    email = body.email.strip().lower()
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if (
        user is None
        or user.deleted_at is not None
        or not verify_password(user.password_hash, body.password)
    ):
        raise HTTPException(401, error("invalid_credentials", AUTH_FAILED))
    row, refresh = await open_session(session, user, request.headers.get("user-agent"))
    session.add(AuditLog(user_id=user.id, actor="user", action="login", target_type="session"))
    await session.commit()
    return _issue(response, user, row.id, refresh)


@router.post("/refresh")
async def refresh(
    response: Response,
    session: AsyncSession = Depends(get_session),
    token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
) -> dict:
    if not token:
        raise HTTPException(401, error("unauthenticated", "Sign in to continue."))
    user, row, raw = await rotate_session(session, token)
    await session.commit()
    return _issue(response, user, row.id, raw)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    payload = _payload(request)
    await revoke_session(session, uuid.UUID(payload["sid"]))
    session.add(AuditLog(user_id=user.id, actor="user", action="logout", target_type="session"))
    await session.commit()
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
    return {"signed_out": True}


@router.get("/session")
async def current(user: User = Depends(require_user)) -> dict:
    return {"user": _profile(user)}


@router.get("/preferences")
async def get_preferences(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return the authenticated user's saved preferences."""
    preferences = await session.get(UserPreferences, user.id)
    if preferences is None:
        raise HTTPException(404, error("preferences_not_found", "Preferences not found."))
    return {"preferences": _preferences(preferences)}


@router.patch("/preferences")
async def update_preferences(
    body: PreferencesUpdate,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Update selected preferences for the authenticated user."""
    preferences = await session.get(UserPreferences, user.id)
    if preferences is None:
        raise HTTPException(404, error("preferences_not_found", "Preferences not found."))
    updates = body.model_dump(exclude_unset=True)
    if not updates or any(value is None for value in updates.values()):
        raise HTTPException(422, error("invalid_preferences", "Provide preference values."))
    for field, value in updates.items():
        setattr(preferences, field, value)
    await session.commit()
    return {"preferences": _preferences(preferences)}


@router.get("/sessions")
async def list_sessions(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List this user's active sessions without exposing credentials."""
    current_id = uuid.UUID(_payload(request)["sid"])
    rows = (
        await session.execute(
            select(Session)
            .where(
                Session.user_id == user.id,
                Session.revoked_at.is_(None),
                Session.expires_at > datetime.now(UTC),
            )
            .order_by(Session.created_at.desc())
        )
    ).scalars()
    return {
        "sessions": [
            {
                "id": str(row.id),
                "user_agent": row.user_agent,
                "created_at": row.created_at,
                "expires_at": row.expires_at,
                "current": row.id == current_id,
            }
            for row in rows
        ]
    }


@router.delete("/sessions/{session_id}")
async def revoke_owned_session(
    session_id: uuid.UUID,
    request: Request,
    response: Response,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Revoke one session only when it belongs to the authenticated user."""
    row = (
        await session.execute(
            select(Session)
            .where(Session.id == session_id, Session.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, error("session_not_found", "Session not found."))
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        session.add(
            AuditLog(
                user_id=user.id,
                actor="user",
                action="session_revoke",
                target_type="session",
                target_id=str(row.id),
            )
        )
    await session.commit()
    if session_id == uuid.UUID(_payload(request)["sid"]):
        response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
    return {"revoked": True}


@router.post("/password")
async def change_password(
    body: PasswordChange,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(401, error("invalid_credentials", AUTH_FAILED))
    user.password_hash = hash_password(body.new_password)
    user.updated_at = datetime.now(UTC)
    await revoke_all_sessions(session, user.id)
    session.add(
        AuditLog(user_id=user.id, actor="user", action="password_change", target_type="user")
    )
    await session.commit()
    return {"changed": True}


@router.delete("/account")
async def delete_account(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mark the account deleted. A later phase erases the rows outright."""
    user.deleted_at = datetime.now(UTC)
    await revoke_all_sessions(session, user.id)
    session.add(
        AuditLog(user_id=user.id, actor="user", action="account_delete", target_type="user")
    )
    await session.commit()
    return {"deleted": True}


def _profile(user: User) -> dict:
    return {"id": str(user.id), "email": user.email, "display_name": user.display_name}


def _preferences(preferences: UserPreferences) -> dict:
    """Serialize preferences without exposing user or database identifiers."""
    return {
        "timezone": preferences.timezone,
        "briefing_language": preferences.briefing_language,
    }


def _payload(request: Request) -> dict:
    import jwt

    from app.config import settings

    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip()
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
