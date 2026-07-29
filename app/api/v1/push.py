"""Web push API (Stage 9).

Endpoints:
  GET    /push/public-key            → the VAPID public key the browser
                                       subscribes with (base64url)
  POST   /push/subscribe             → register a (user, restaurant,
                                       endpoint) subscription
  DELETE /push/subscribe             → remove by endpoint
"""
from __future__ import annotations

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps.auth import get_current_user
from app.models.user import User
from app.services import push as push_service

router = APIRouter(prefix="/push", tags=["push"])


class PublicKeyOut(BaseModel):
    public_key: Optional[str]


class SubscribeIn(BaseModel):
    restaurant_id: uuid.UUID
    endpoint: str
    keys: dict  # {"p256dh": ..., "auth": ...}
    user_agent: Optional[str] = None


class SubscribeOut(BaseModel):
    ok: bool


class UnsubscribeIn(BaseModel):
    endpoint: str


@router.get("/public-key", response_model=PublicKeyOut)
async def get_vapid_public_key() -> PublicKeyOut:
    return PublicKeyOut(public_key=push_service.get_public_vapid_key())


@router.post("/subscribe", response_model=SubscribeOut)
async def subscribe(
    body: SubscribeIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> SubscribeOut:
    p256dh = body.keys.get("p256dh")
    auth = body.keys.get("auth")
    if not p256dh or not auth:
        raise HTTPException(status_code=400, detail="keys.p256dh and keys.auth are required")
    await push_service.subscribe(
        db, user_id=actor.id, restaurant_id=body.restaurant_id,
        endpoint=body.endpoint, p256dh=p256dh, auth=auth,
        user_agent=body.user_agent,
    )
    return SubscribeOut(ok=True)


@router.delete("/subscribe", response_model=SubscribeOut)
async def unsubscribe(
    body: UnsubscribeIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> SubscribeOut:
    ok = await push_service.unsubscribe(
        db, endpoint=body.endpoint, user_id=actor.id,
    )
    return SubscribeOut(ok=ok)
