"""Web push service — Stage 9.

Uses `pywebpush` (VAPID) for actual delivery. Keys are auto-generated
on first boot if not in env, and persisted to `vapid_keys.json` so
subscribers don't get invalidated on container restart.

Why a service-side keyfile rather than env:
  - VAPID private keys are too long for a typical env var
  - Generating fresh on every restart would invalidate all browser
    subscriptions (browsers tie the subscription to a specific public
    key). Persisting to disk makes the keys stable across restarts.

For production on Azure Container Apps, prefer pulling from Key Vault
or mounting a file (see AZURE_DEPLOY_CHECKLIST.md).

The push trigger lives here too: `notify_deal_approved(deal, restaurant)`
finds every push subscription for the deal's restaurant and sends a
push. Called from the deal-approval endpoint.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import webpush, WebPushException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.deal import Deal
from app.models.push import PushSubscription
from app.models.restaurant import Restaurant
from app.services.sharing import deal_share_url

logger = logging.getLogger("halalistic.push")

# Local file where we persist VAPID keys if not in env. Resolved relative
# to the repo root so the same file is picked up across reloads.
_VAPID_KEYFILE = Path(__file__).resolve().parents[2] / "vapid_keys.json"


def _public_key_b64url(public_key) -> str:
    """Browser-friendly VAPID public key: base64url-encoded uncompressed
    P-256 point (65 bytes: 0x04 || X || Y). py_vapid exposes the raw
    EllipticCurvePublicKey; we serialise it ourselves.
    """
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# ---------- VAPID key bootstrap ----------
def _load_or_create_vapid_keys() -> tuple[str, str]:
    """Return (private_pem, public_b64url). Order: env → keyfile → fresh.

    Persists freshly-generated keys to disk so restarts don't invalidate
    browser subscriptions. In production on Azure, prefer mounting a
    pre-generated keyfile or pulling from Key Vault.
    """
    if settings.vapid_private_key and settings.vapid_public_key:
        return settings.vapid_private_key, settings.vapid_public_key
    if _VAPID_KEYFILE.is_file():
        try:
            data = json.loads(_VAPID_KEYFILE.read_text(encoding="utf-8"))
            return data["private_pem"], data["public_b64url"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("vapid keyfile unreadable (%s); regenerating", exc)
    logger.info("VAPID keys not in env; generating a new pair (one-time).")
    v = Vapid()
    v.generate_keys()
    private_pem = v.private_pem()
    if isinstance(private_pem, bytes):
        private_pem = private_pem.decode("utf-8")
    public_b64url = _public_key_b64url(v.public_key)
    try:
        _VAPID_KEYFILE.write_text(
            json.dumps({"private_pem": private_pem, "public_b64url": public_b64url}),
            encoding="utf-8",
        )
        os.chmod(_VAPID_KEYFILE, 0o600)
        logger.info("VAPID keys written to %s", _VAPID_KEYFILE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not persist VAPID keys to disk (%s)", exc)
    return private_pem, public_b64url


# ---------- Subscribe / unsubscribe ----------
async def subscribe(
    db: AsyncSession, *,
    user_id,
    restaurant_id,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: Optional[str] = None,
) -> PushSubscription:
    """Idempotent: if the same endpoint already exists, return the
    existing row; otherwise insert a new one.
    """
    existing = await db.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    )
    if existing is not None:
        return existing
    row = PushSubscription(
        id=__import__("uuid").uuid4(),
        user_id=user_id,
        restaurant_id=restaurant_id,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
        user_agent=user_agent,
    )
    db.add(row)
    try:
        await db.commit()
        await db.refresh(row)
    except Exception:  # noqa: BLE001
        await db.rollback()
        # Race: another request inserted the same endpoint. Re-read.
        existing = await db.scalar(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        )
        if existing is not None:
            return existing
        raise
    return row


async def unsubscribe(db: AsyncSession, *, endpoint: str, user_id) -> bool:
    """Remove a subscription. Returns True if a row was deleted."""
    row = await db.scalar(
        select(PushSubscription).where(
            PushSubscription.endpoint == endpoint,
            PushSubscription.user_id == user_id,
        )
    )
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


# ---------- Trigger ----------
async def notify_deal_approved(
    db: AsyncSession, *, deal: Deal, restaurant: Restaurant,
) -> int:
    """Find every push subscription for this restaurant and send a push
    notification announcing the new approved deal. Returns the number
    of notifications successfully queued (or attempted).
    """
    rows = list((await db.execute(
        select(PushSubscription).where(PushSubscription.restaurant_id == restaurant.id)
    )).scalars().all())
    if not rows:
        return 0
    private_pem, public_b64url = _load_or_create_vapid_keys()
    share_url = deal_share_url(deal.id)
    payload = json.dumps({
        "title": f"New deal at {restaurant.name}",
        "body": deal.title,
        "url": share_url,
        "deal_id": str(deal.id),
        "restaurant_id": str(restaurant.id),
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    sent = 0
    for sub in rows:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=private_pem,
                vapid_claims={
                    "sub": settings.vapid_claims_email,
                },
            )
            sent += 1
        except WebPushException as exc:
            # 404 / 410 = the subscription is gone (user uninstalled
            # the service-worker or revoked permission). Remove the row
            # so we don't keep retrying. Other errors → log + leave.
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in (404, 410):
                logger.info("push endpoint %s gone (status=%s); removing",
                            sub.endpoint[:60], status_code)
                await db.delete(sub)
                await db.commit()
            else:
                logger.warning("push send failed (endpoint=%s status=%s): %s",
                               sub.endpoint[:60], status_code, exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("push send unexpected error: %s", exc)
    if sent:
        logger.info("deal-approved push: sent=%d restaurant_id=%s",
                    sent, restaurant.id)
    return sent


# ---------- Public VAPID public key endpoint ----------
def get_public_vapid_key() -> Optional[str]:
    """Return the VAPID public key in the URL-safe base64 format the
    browser Push API expects. None only if the env-var path is set but
    empty (in which case the auto-bootstrap on first send generates
    one and the frontend will get a 503-ish on this endpoint until the
    first push is queued).
    """
    if settings.vapid_public_key:
        return settings.vapid_public_key
    if _VAPID_KEYFILE.is_file():
        try:
            data = json.loads(_VAPID_KEYFILE.read_text(encoding="utf-8"))
            return data.get("public_b64url")
        except Exception:  # noqa: BLE001
            return None
    return None
