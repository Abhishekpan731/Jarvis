"""Device registration and revocation.

A device identifies a client for sync attribution. It is not a credential: every
request is still authenticated by the user's token, and a device id presented by
another account is rejected.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import AuditLog, Device, User
from app.security import error, require_user

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])

PLATFORMS = ("ios", "android", "web", "desktop", "api")


class DeviceIn(BaseModel):
    platform: str
    label: str = Field(min_length=1, max_length=80)


def _dump(row: Device) -> dict:
    return {
        "id": str(row.id),
        "platform": row.platform,
        "label": row.label,
        "created_at": row.created_at.isoformat(),
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "revoked": row.revoked_at is not None,
    }


@router.post("", status_code=201)
async def register(
    body: DeviceIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if body.platform not in PLATFORMS:
        raise HTTPException(422, error("invalid_platform", "Unknown device platform."))
    row = Device(id=uuid.uuid4(), user_id=user.id, platform=body.platform, label=body.label)
    session.add(row)
    session.add(
        AuditLog(
            user_id=user.id,
            actor="user",
            action="device_register",
            target_type="device",
            target_id=str(row.id),
        )
    )
    await session.commit()
    return _dump(row)


@router.get("")
async def list_devices(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        (
            await session.execute(
                select(Device).where(Device.user_id == user.id).order_by(Device.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {"devices": [_dump(row) for row in rows]}


@router.delete("/{device_id}")
async def revoke(
    device_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Device, device_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, error("not_found", "No device with that id exists for this account."))
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        session.add(
            AuditLog(
                user_id=user.id,
                actor="user",
                action="device_revoke",
                target_type="device",
                target_id=str(row.id),
            )
        )
        await session.commit()
    return {"revoked": True}
