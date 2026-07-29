"""Tests for Stage 7: Stripe billing + webhooks + tier enforcement.

Coverage:
  - 5 webhook event types (checkout.session.completed, subscription.created,
    .updated, .deleted, invoice.payment_failed)
  - 2 signature-bypass attempts (unsigned + bad signature → 400)
  - 2 user-subscription tier transitions (active → premium, deleted → free)
  - 1 tier-downgrade-revokes-push-only-access end-to-end
  - 1 photo-cap-reads-from-current-tier end-to-end
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import settings
from app.core.security import hash_password
from app.models.billing import RestaurantBillingSubscription, UserBillingSubscription
from app.models.deal import Deal
from app.models.enums import (
    DealAudience,
    DealStatus,
    DealType,
    RestaurantTier,
    UserRole,
)
from app.models.restaurant import Restaurant
from app.models.user import User
from app.services import billing as billing_service
from app.services.deals import create_hand_curated  # for the push-only gate test
from app.services.deals import CreateDealInput


# ---- helpers ----
async def _make_owner(db, email: str) -> User:
    u = User(
        id=uuid.uuid4(), email=email, password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=UserRole.RESTAURANT_OWNER,
    )
    db.add(u); await db.commit(); await db.refresh(u)
    return u


async def _make_diner(db, email: str) -> User:
    u = User(
        id=uuid.uuid4(), email=email, password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=UserRole.DINER,
    )
    db.add(u); await db.commit(); await db.refresh(u)
    return u


async def _make_curator(db, email: str) -> User:
    u = User(
        id=uuid.uuid4(), email=email, password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=UserRole.DEAL_CURATOR,
    )
    db.add(u); await db.commit(); await db.refresh(u)
    return u


async def _make_admin(db, email: str) -> User:
    u = User(
        id=uuid.uuid4(), email=email, password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=UserRole.PLATFORM_ADMIN,
    )
    db.add(u); await db.commit(); await db.refresh(u)
    return u


async def _mint_token(db, email: str) -> str:
    from app.services.auth_service import login as svc_login
    _, pair = await svc_login(db, email, "supersecret123")
    return pair.access_token


async def _make_restaurant(client, token, name: str = "R") -> str:
    r = await client.post(
        "/api/v1/restaurants",
        json={"name": name, "address_line": "1 St", "city": "Houston",
              "state": "TX", "postal_code": "77002", "country": "US",
              "price_range": "2", "cuisine_slugs": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _force_tier(db, restaurant_id, tier: RestaurantTier) -> None:
    """Direct DB nudge for tests that don't go through the full billing path."""
    from sqlalchemy import update
    from tests.conftest import _test_engine
    async with _test_engine.begin() as conn:
        await conn.execute(
            update(Restaurant).where(Restaurant.id == restaurant_id).values(tier=tier)
        )


def _event(etype: str, **data) -> dict:
    """Build a Stripe-style event dict."""
    return {
        "id": f"evt_test_{uuid.uuid4().hex[:12]}",
        "type": etype,
        "data": {"object": data},
    }


def _checkout_session(
    *, kind: str, owner_id: uuid.UUID, customer_id: str = "cus_test",
    subscription_id: str = "sub_test", target_tier: str = "premium",
) -> dict:
    return {
        "id": f"cs_test_{uuid.uuid4().hex[:12]}",
        "object": "checkout.session",
        "customer": customer_id,
        "subscription": subscription_id,
        "metadata": {"kind": kind, "restaurant_id" if kind == "restaurant" else "user_id": str(owner_id),
                     **({"target_tier": target_tier} if kind == "restaurant" else {})},
    }


def _subscription(
    *, kind: str, owner_id: uuid.UUID, status: str = "active",
    target_tier: str = "premium", subscription_id: str = "sub_test",
    customer_id: str = "cus_test", cancel_at_period_end: bool = False,
    current_period_end_ts: int | None = None,
) -> dict:
    if current_period_end_ts is None:
        current_period_end_ts = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    md = {"kind": kind}
    if kind == "restaurant":
        md["restaurant_id"] = str(owner_id)
        md["target_tier"] = target_tier
    else:
        md["user_id"] = str(owner_id)
    return {
        "id": subscription_id,
        "object": "subscription",
        "status": status,
        "customer": customer_id,
        "cancel_at_period_end": cancel_at_period_end,
        "current_period_end": current_period_end_ts,
        "metadata": md,
    }


def _invoice(*, subscription_id: str = "sub_test") -> dict:
    return {
        "id": f"in_test_{uuid.uuid4().hex[:12]}",
        "object": "invoice",
        "subscription": subscription_id,
    }


# ---- Webhook event tests (call service directly, no HTTP) ----
@pytest.mark.asyncio
async def test_subscription_created_activates_premium_tier(client: AsyncClient, db):
    """customer.subscription.created with status=active + target_tier=premium → restaurant.tier=premium."""
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    rid = await _make_restaurant(client, owner_token)
    rid_uuid = uuid.UUID(rid)

    # Simulate checkout first (creates the row with stripe_customer_id).
    await billing_service.handle_event(db, _event(
        "checkout.session.completed",
        **_checkout_session(kind="restaurant", owner_id=rid_uuid, target_tier="premium"),
    ))
    # Then the subscription.created event with active status.
    await billing_service.handle_event(db, _event(
        "customer.subscription.created",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="active", target_tier="premium"),
    ))

    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.PREMIUM
    row = await db.scalar(select(RestaurantBillingSubscription).where(
        RestaurantBillingSubscription.restaurant_id == rid_uuid))
    assert row is not None
    assert row.status == "active"
    assert row.tier == "premium"


@pytest.mark.asyncio
async def test_subscription_past_due_keeps_tier(client: AsyncClient, db):
    """past_due is the grace window during Stripe's payment retry. Tier stays."""
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    rid = await _make_restaurant(client, owner_token)
    rid_uuid = uuid.UUID(rid)

    # Activate first.
    await billing_service.handle_event(db, _event(
        "customer.subscription.created",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="active", target_tier="featured"),
    ))
    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.FEATURED

    # Then status flips to past_due — tier should stay.
    await billing_service.handle_event(db, _event(
        "customer.subscription.updated",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="past_due", target_tier="featured"),
    ))
    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.FEATURED, "past_due is the grace window; tier should NOT downgrade"
    row = await db.scalar(select(RestaurantBillingSubscription).where(
        RestaurantBillingSubscription.restaurant_id == rid_uuid))
    assert row.status == "past_due"


@pytest.mark.asyncio
async def test_subscription_unpaid_downgrades_to_free(client: AsyncClient, db):
    """status=unpaid → downgrade to free (the DoD: a lapsed sub must downgrade access)."""
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    rid = await _make_restaurant(client, owner_token)
    rid_uuid = uuid.UUID(rid)

    # Activate first.
    await billing_service.handle_event(db, _event(
        "customer.subscription.created",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="active", target_tier="premium"),
    ))
    # Then unpaid.
    await billing_service.handle_event(db, _event(
        "customer.subscription.updated",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="unpaid", target_tier="premium"),
    ))
    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.FREE, "unpaid must downgrade tier immediately"
    row = await db.scalar(select(RestaurantBillingSubscription).where(
        RestaurantBillingSubscription.restaurant_id == rid_uuid))
    assert row.status == "unpaid"
    assert row.tier == "free"


@pytest.mark.asyncio
async def test_subscription_deleted_downgrades_to_free(client: AsyncClient, db):
    """customer.subscription.deleted → tier=free (the canonical cancel path)."""
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    rid = await _make_restaurant(client, owner_token)
    rid_uuid = uuid.UUID(rid)

    await billing_service.handle_event(db, _event(
        "customer.subscription.created",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="active", target_tier="premium"),
    ))
    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.PREMIUM

    await billing_service.handle_event(db, _event(
        "customer.subscription.deleted",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="canceled", target_tier="premium"),
    ))
    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.FREE
    row = await db.scalar(select(RestaurantBillingSubscription).where(
        RestaurantBillingSubscription.restaurant_id == rid_uuid))
    assert row.status == "canceled"
    assert row.tier == "free"


@pytest.mark.asyncio
async def test_invoice_payment_failed_marks_past_due(client: AsyncClient, db):
    """invoice.payment_failed → status=past_due, tier stays (grace window)."""
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    rid = await _make_restaurant(client, owner_token)
    rid_uuid = uuid.UUID(rid)

    await billing_service.handle_event(db, _event(
        "customer.subscription.created",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="active", target_tier="featured"),
    ))
    await billing_service.handle_event(db, _event(
        "invoice.payment_failed",
        **_invoice(subscription_id="sub_test"),
    ))
    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.FEATURED
    row = await db.scalar(select(RestaurantBillingSubscription).where(
        RestaurantBillingSubscription.restaurant_id == rid_uuid))
    assert row.status == "past_due"


@pytest.mark.asyncio
async def test_cancel_at_period_end_keeps_tier_until_deleted(client: AsyncClient, db):
    """cancel_at_period_end=true with status=active → keep tier; only deleted downgrades."""
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    rid = await _make_restaurant(client, owner_token)
    rid_uuid = uuid.UUID(rid)

    await billing_service.handle_event(db, _event(
        "customer.subscription.created",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="active", target_tier="premium"),
    ))
    # User clicks "cancel" in the portal — Stripe flips cancel_at_period_end but
    # keeps status=active until the period ends.
    await billing_service.handle_event(db, _event(
        "customer.subscription.updated",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="active",
                        target_tier="premium", cancel_at_period_end=True),
    ))
    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.PREMIUM, "tier stays until the period actually ends"

    # Period ends, Stripe fires the deleted event.
    await billing_service.handle_event(db, _event(
        "customer.subscription.deleted",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="canceled", target_tier="premium"),
    ))
    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.FREE


# ---- Signature bypass tests (HTTP layer) ----
def _sign_payload(payload: bytes, secret: str, ts: int | None = None) -> str:
    """Build a Stripe-style signature header for tests."""
    if ts is None:
        ts = int(time.time())
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


@pytest.mark.asyncio
async def test_webhook_unsigned_rejected(client: AsyncClient):
    """No Stripe-Signature header → 400 (and no event processed)."""
    r = await client.post(
        "/api/v1/webhooks/stripe",
        content=b'{"type":"customer.subscription.deleted","data":{"object":{}}}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_webhook_bad_signature_rejected(client: AsyncClient, db, monkeypatch):
    """Wrong signature → 400. Use a known test secret so signature verification is exercised."""
    # Set the webhook secret to something deterministic for the test.
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test_secret_for_billing_tests")
    payload = b'{"type":"customer.subscription.deleted","data":{"object":{}}}'
    r = await client.post(
        "/api/v1/webhooks/stripe",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": "t=1,v1=deadbeef0000000000000000000000000000000000000000000000000000beef",
        },
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_webhook_valid_signature_accepted(client: AsyncClient, db, monkeypatch):
    """A correctly signed payload is accepted (200) and processed."""
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test_secret_for_billing_tests")
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    # Hit /restaurants to create one in this test session.
    from httpx import ASGITransport
    # We need to create a restaurant through the API. Use the existing client.
    create = await client.post(
        "/api/v1/restaurants",
        json={"name": "X", "address_line": "1 St", "city": "Houston",
              "state": "TX", "postal_code": "77002", "country": "US",
              "price_range": "2", "cuisine_slugs": []},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert create.status_code == 201
    rid = uuid.UUID(create.json()["id"])

    payload_dict = _event(
        "customer.subscription.created",
        **_subscription(kind="restaurant", owner_id=rid, status="active", target_tier="premium"),
    )
    payload = json.dumps(payload_dict).encode()
    sig = _sign_payload(payload, "whsec_test_secret_for_billing_tests")
    r = await client.post(
        "/api/v1/webhooks/stripe",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": sig,
        },
    )
    assert r.status_code == 200, r.text
    r2 = await db.get(Restaurant, rid)
    assert r2.tier == RestaurantTier.PREMIUM


# ---- User subscription tests ----
@pytest.mark.asyncio
async def test_user_subscription_active_grants_premium_tier(client: AsyncClient, db):
    """User subscribes → get_user_subscription_tier returns 'premium'."""
    user = await _make_diner(db, f"din-{uuid.uuid4().hex[:6]}@example.com")
    uid = user.id
    # Simulate checkout + subscription.created.
    await billing_service.handle_event(db, _event(
        "checkout.session.completed",
        **_checkout_session(kind="user", owner_id=uid),
    ))
    await billing_service.handle_event(db, _event(
        "customer.subscription.created",
        **_subscription(kind="user", owner_id=uid, status="active"),
    ))
    tier = await billing_service.get_user_subscription_tier(db, uid)
    assert tier == "premium"


@pytest.mark.asyncio
async def test_user_subscription_deleted_returns_free(client: AsyncClient, db):
    """User subscription canceled → get_user_subscription_tier returns 'free'."""
    user = await _make_diner(db, f"din-{uuid.uuid4().hex[:6]}@example.com")
    uid = user.id
    await billing_service.handle_event(db, _event(
        "customer.subscription.created",
        **_subscription(kind="user", owner_id=uid, status="active"),
    ))
    assert await billing_service.get_user_subscription_tier(db, uid) == "premium"
    await billing_service.handle_event(db, _event(
        "customer.subscription.deleted",
        **_subscription(kind="user", owner_id=uid, status="canceled"),
    ))
    assert await billing_service.get_user_subscription_tier(db, uid) == "free"


# ---- Tier-downgrade-revokes-push-only-access (end-to-end DoD #2) ----
@pytest.mark.asyncio
async def test_tier_downgrade_revokes_push_only_access(client: AsyncClient, db):
    """A premium restaurant can create push-only deals. After subscription
    cancellation, the SAME restaurant is blocked from creating new push-only
    deals. Existing push-only deals are NOT auto-expired (that's a separate
    Phase 2 task per BRD).
    """
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    curator = await _make_curator(db, f"cur-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    curator_token = await _mint_token(db, curator.email)
    rid = await _make_restaurant(client, owner_token, "Push Test")
    rid_uuid = uuid.UUID(rid)

    # Activate premium tier via webhook.
    await billing_service.handle_event(db, _event(
        "customer.subscription.created",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="active", target_tier="premium"),
    ))
    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.PREMIUM

    # Curator can create a push-only deal on this premium restaurant.
    r = await client.post(
        f"/api/v1/admin/deals/create",
        params={"restaurant_id": rid},
        json={"title": "VIP", "deal_type": "percentage_off", "discount_value": "20.00",
              "start_date": datetime.now(timezone.utc).date().isoformat(),
              "end_date": (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat(),
              "target_audience": "push_only"},
        headers={"Authorization": f"Bearer {curator_token}"},
    )
    assert r.status_code == 201, r.text

    # Now cancel the subscription — tier downgrades to free.
    await billing_service.handle_event(db, _event(
        "customer.subscription.deleted",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="canceled", target_tier="premium"),
    ))
    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.FREE

    # Same curator + same restaurant — now blocked from push-only.
    r = await client.post(
        f"/api/v1/admin/deals/create",
        params={"restaurant_id": rid},
        json={"title": "VIP2", "deal_type": "percentage_off", "discount_value": "20.00",
              "start_date": datetime.now(timezone.utc).date().isoformat(),
              "end_date": (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat(),
              "target_audience": "push_only"},
        headers={"Authorization": f"Bearer {curator_token}"},
    )
    assert r.status_code == 400, r.text
    assert "premium" in r.json()["detail"].lower()


# ---- Photo-cap reads from current tier (DoD #1, end-to-end) ----
@pytest.mark.asyncio
async def test_photo_cap_reads_from_current_tier_after_downgrade(client: AsyncClient, db):
    """A premium restaurant can hold 10 photos. After subscription cancel,
    the cap drops to 2 (free tier). A 3rd photo upload is then rejected.

    For brevity in the test we use the service directly: create 2 photos,
    then attempt a 3rd upload at the free tier and assert 409.
    """
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    rid = await _make_restaurant(client, owner_token, "Photo Cap Test")
    rid_uuid = uuid.UUID(rid)

    # Activate premium.
    await billing_service.handle_event(db, _event(
        "customer.subscription.created",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="active", target_tier="premium"),
    ))
    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.PREMIUM

    # Cancel — tier drops to free (cap=2).
    await billing_service.handle_event(db, _event(
        "customer.subscription.deleted",
        **_subscription(kind="restaurant", owner_id=rid_uuid, status="canceled", target_tier="premium"),
    ))
    r = await db.get(Restaurant, rid_uuid)
    assert r.tier == RestaurantTier.FREE

    # Service-level check: a 3rd photo would now exceed the free cap.
    from app.services.photos import PhotoService
    assert PhotoService.cap_for_tier(r.tier) == 2

    # HTTP-level: upload 2 photos (under cap), then assert 3rd is rejected.
    import io
    from PIL import Image
    def _png():
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), color=(0, 128, 0)).save(buf, format="PNG")
        return buf.getvalue()
    for i in range(2):
        up = await client.post(
            f"/api/v1/restaurants/{rid}/photos",
            files={"file": (f"a{i}.png", io.BytesIO(_png()), "image/png")},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert up.status_code == 201, f"photo {i} should succeed at free cap of 2: {up.text}"

    up3 = await client.post(
        f"/api/v1/restaurants/{rid}/photos",
        files={"file": ("a3.png", io.BytesIO(_png()), "image/png")},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert up3.status_code == 409, f"3rd photo at free cap should be 409, got {up3.text}"
