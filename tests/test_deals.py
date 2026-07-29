"""Tests for Stage 6: deals — full state machine, visibility, auto-expiry, role gates.

Coverage:
  - 7 valid transitions
  - 11 invalid transitions (parameterized)
  - Auto-expiry (scheduled-job function called directly)
  - Visibility rules (public vs push-only, premium-tier gate)
  - Role gates (owner can't self-publish, non-curator can't moderate)
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security import hash_password
from app.models.deal import Deal, RestaurantPushSubscription as RestaurantSubscription
from app.models.enums import (
    DealAudience,
    DealStatus,
    DealType,
    RestaurantTier,
    UserRole,
)
from app.models.restaurant import Restaurant
from app.models.user import User
from app.services import deals as deal_service


# ---- helpers ----
async def _make_diner(db, email: str) -> User:
    u = User(
        id=uuid.uuid4(), email=email, password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=UserRole.DINER,
    )
    db.add(u); await db.commit(); await db.refresh(u)
    return u


async def _make_owner(db, email: str) -> User:
    u = User(
        id=uuid.uuid4(), email=email, password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=UserRole.RESTAURANT_OWNER,
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


async def _make_restaurant(
    client, token, name: str = "R", tier: RestaurantTier = RestaurantTier.FREE
) -> str:
    r = await client.post(
        "/api/v1/restaurants",
        json={"name": name, "address_line": "1 St", "city": "Houston",
              "state": "TX", "postal_code": "77002", "country": "US",
              "price_range": "2", "cuisine_slugs": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    if tier != RestaurantTier.FREE:
        # Tier is admin-only; flip it directly in the DB for the test.
        from app.models.restaurant import Restaurant
        rest = await client.get(
            f"/api/v1/restaurants/{rid}",
        ) if False else None
        # We don't have a public endpoint that returns the ORM, so do it
        # via the engine's session.
        from tests.conftest import _test_engine
        from sqlalchemy import update
        async with _test_engine.begin() as conn:
            await conn.execute(
                update(Restaurant).where(Restaurant.id == uuid.UUID(rid)).values(tier=tier)
            )
    return rid


def _good_deal_body(**over) -> dict:
    today = date.today()
    return {
        "title": "20% off your next meal",
        "description": "Valid any weekday, dine-in only.",
        "deal_type": DealType.PERCENTAGE_OFF.value,
        "discount_value": "20.00",
        "start_date": today.isoformat(),
        "end_date": (today + timedelta(days=30)).isoformat(),
        "target_audience": DealAudience.PUBLIC.value,
        **over,
    }


# ---- valid transitions (7) ----
@pytest.mark.asyncio
async def test_valid_create_draft_then_submit_then_approve(client: AsyncClient, db):
    """Valid chain: (none) -> draft -> pending_review -> approved."""
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    curator = await _make_curator(db, f"cur-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    curator_token = await _mint_token(db, curator.email)
    rid = await _make_restaurant(client, owner_token)

    # (none) -> draft
    r = await client.post(
        f"/api/v1/restaurants/{rid}/deals",
        json=_good_deal_body(),
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert r.status_code == 201, r.text
    deal_id = r.json()["id"]
    assert r.json()["status"] == DealStatus.DRAFT.value

    # draft -> pending_review
    r = await client.post(
        f"/api/v1/deals/{deal_id}/submit",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == DealStatus.PENDING_REVIEW.value

    # pending_review -> approved
    r = await client.post(
        f"/api/v1/admin/deals/{deal_id}/approve",
        headers={"Authorization": f"Bearer {curator_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == DealStatus.APPROVED.value
    assert r.json()["curator_created"] is False


@pytest.mark.asyncio
async def test_valid_rejected_to_draft_then_resubmit(client: AsyncClient, db):
    """Valid chain: pending_review -> rejected -> draft -> pending_review -> approved."""
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    curator = await _make_curator(db, f"cur-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    curator_token = await _mint_token(db, curator.email)
    rid = await _make_restaurant(client, owner_token)

    r = await client.post(
        f"/api/v1/restaurants/{rid}/deals",
        json=_good_deal_body(),
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    deal_id = r.json()["id"]
    await client.post(f"/api/v1/deals/{deal_id}/submit", headers={"Authorization": f"Bearer {owner_token}"})
    rej = await client.post(
        f"/api/v1/admin/deals/{deal_id}/reject",
        json={"reason": "title is too vague"},
        headers={"Authorization": f"Bearer {curator_token}"},
    )
    assert rej.status_code == 200
    assert rej.json()["status"] == DealStatus.REJECTED.value
    assert "vague" in rej.json()["rejection_reason"].lower()

    # rejected -> draft
    rev = await client.post(
        f"/api/v1/deals/{deal_id}/revise",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert rev.status_code == 200
    assert rev.status_code == 200
    assert rev.json()["status"] == DealStatus.DRAFT.value

    # Re-submit + approve
    await client.post(f"/api/v1/deals/{deal_id}/submit", headers={"Authorization": f"Bearer {owner_token}"})
    appr = await client.post(
        f"/api/v1/admin/deals/{deal_id}/approve",
        headers={"Authorization": f"Bearer {curator_token}"},
    )
    assert appr.json()["status"] == DealStatus.APPROVED.value


@pytest.mark.asyncio
async def test_valid_curator_creates_at_approved(client: AsyncClient, db):
    """Valid: (none) -> approved via curator hand-curation, skips review."""
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    curator = await _make_curator(db, f"cur-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    curator_token = await _mint_token(db, curator.email)
    rid = await _make_restaurant(client, owner_token)
    r = await client.post(
        f"/api/v1/admin/deals/create",
        params={"restaurant_id": rid},
        json=_good_deal_body(title="VIP 30% off, hand-curated"),
        headers={"Authorization": f"Bearer {curator_token}"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == DealStatus.APPROVED.value
    assert r.json()["curator_created"] is True


@pytest.mark.asyncio
async def test_valid_approved_to_expired_via_scheduler(client: AsyncClient, db):
    """Valid: approved -> expired via the auto-expiry function (BRD §3.5)."""
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    curator = await _make_curator(db, f"cur-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    curator_token = await _mint_token(db, curator.email)
    rid = await _make_restaurant(client, owner_token)

    # Create a normal in-range deal via the curator fast-path, then backdate
    # it by adjusting BOTH start_date and end_date in the DB (bypassing the
    # API's "end_date >= today" validator + the DB's "end_date >= start_date"
    # check constraint).
    r = await client.post(
        f"/api/v1/admin/deals/create",
        params={"restaurant_id": rid},
        json=_good_deal_body(title="Backdated for test"),
        headers={"Authorization": f"Bearer {curator_token}"},
    )
    assert r.status_code == 201, r.text
    deal_id = r.json()["id"]
    assert r.json()["status"] == DealStatus.APPROVED.value

    d = await db.get(Deal, uuid.UUID(deal_id))
    d.start_date = date.today() - timedelta(days=10)
    d.end_date = date.today() - timedelta(days=1)
    await db.commit()

    # Run the auto-expiry
    n = await deal_service.expire_old_deals(db)
    assert n == 1

    # Reload and assert status is now expired
    d = await db.get(Deal, uuid.UUID(deal_id))
    assert d.status == DealStatus.EXPIRED.value


# ---- invalid transitions (parameterized) ----
# Each entry: (current_status, target_status, why)
INVALID_TRANSITIONS = [
    (DealStatus.DRAFT, DealStatus.APPROVED, "no skip-to-approved for owner"),
    (DealStatus.DRAFT, DealStatus.REJECTED, "no skip-to-rejected for owner"),
    (DealStatus.DRAFT, DealStatus.EXPIRED, "no skip-to-expired"),
    (DealStatus.PENDING_REVIEW, DealStatus.DRAFT, "no rollback from pending"),
    (DealStatus.PENDING_REVIEW, DealStatus.EXPIRED, "no skip-to-expired from pending"),
    (DealStatus.APPROVED, DealStatus.DRAFT, "no rollback from approved"),
    (DealStatus.APPROVED, DealStatus.PENDING_REVIEW, "no resubmit from approved"),
    (DealStatus.APPROVED, DealStatus.REJECTED, "no revoke in MVP"),
    (DealStatus.REJECTED, DealStatus.APPROVED, "must go back through draft"),
    (DealStatus.REJECTED, DealStatus.PENDING_REVIEW, "no skip back to pending"),
    (DealStatus.REJECTED, DealStatus.EXPIRED, "no skip to expired"),
    (DealStatus.EXPIRED, DealStatus.DRAFT, "expired is terminal"),
    (DealStatus.EXPIRED, DealStatus.PENDING_REVIEW, "expired is terminal"),
    (DealStatus.EXPIRED, DealStatus.APPROVED, "expired is terminal"),
    (DealStatus.EXPIRED, DealStatus.REJECTED, "expired is terminal"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("from_status,to_status,why", INVALID_TRANSITIONS)
async def test_invalid_transitions_rejected(client: AsyncClient, db, from_status, to_status, why):
    """Each invalid transition must raise 409 (service-level guard)."""
    # Build a Deal row directly in the target FROM status, so we can test
    # the service-level transition guard without going through the full
    # valid path.
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    rid = await _make_restaurant(client, await _mint_token(db, owner.email))
    r = await client.post(
        f"/api/v1/restaurants/{rid}/deals",
        json=_good_deal_body(),
        headers={"Authorization": f"Bearer {await _mint_token(db, owner.email)}"},
    )
    assert r.status_code == 201
    deal = await db.get(Deal, uuid.UUID(r.json()["id"]))
    # Force the deal into the FROM state. We bypass the state machine for
    # setup because we're explicitly testing the transition guard, not the
    # path TO this state.
    deal.status = from_status.value
    await db.commit()
    await db.refresh(deal)

    # Try to transition. Every invalid attempt should raise 409.
    from fastapi import HTTPException
    target_enum = DealStatus(to_status)
    if target_enum == DealStatus.PENDING_REVIEW:
        with pytest.raises(HTTPException) as ei:
            await deal_service.submit(db, deal=deal, actor=owner)
        assert ei.value.status_code == 409
    elif target_enum == DealStatus.APPROVED:
        curator = await _make_curator(db, f"cur-{uuid.uuid4().hex[:6]}@example.com")
        with pytest.raises(HTTPException) as ei:
            await deal_service.approve(db, deal=deal, curator=curator)
        assert ei.value.status_code == 409
    elif target_enum == DealStatus.REJECTED:
        curator = await _make_curator(db, f"cur-{uuid.uuid4().hex[:6]}@example.com")
        with pytest.raises(HTTPException) as ei:
            await deal_service.reject(db, deal=deal, curator=curator, reason="x")
        assert ei.value.status_code == 409
    elif target_enum == DealStatus.DRAFT:
        with pytest.raises(HTTPException) as ei:
            await deal_service.revise(db, deal=deal, actor=owner)
        assert ei.value.status_code == 409
    elif target_enum == DealStatus.EXPIRED:
        # The public-facing transition guard rejects manual EXPIRED moves.
        # (Auto-expiry is the only valid source; it bypasses _validate_transition
        # because it acts on multiple rows in a single SQL UPDATE.)
        from app.services.deals import _validate_transition
        with pytest.raises(HTTPException) as ei:
            _validate_transition(deal, DealStatus.EXPIRED)
        assert ei.value.status_code == 409


# ---- Role gates ----
@pytest.mark.asyncio
async def test_owner_cannot_self_publish(client: AsyncClient, db):
    """Owner cannot directly approve their own deal — must go through a Curator."""
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    rid = await _make_restaurant(client, owner_token)
    r = await client.post(
        f"/api/v1/restaurants/{rid}/deals",
        json=_good_deal_body(),
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    deal_id = r.json()["id"]

    # Owner tries to hit the curator-only approve endpoint -> 403
    appr = await client.post(
        f"/api/v1/admin/deals/{deal_id}/approve",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert appr.status_code == 403, appr.text

    # Owner tries to hit the curator-only reject endpoint -> 403
    rej = await client.post(
        f"/api/v1/admin/deals/{deal_id}/reject",
        json={"reason": "trying"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert rej.status_code == 403


@pytest.mark.asyncio
async def test_non_curator_cannot_hit_admin_endpoints(client: AsyncClient, db):
    diner = await _make_diner(db, f"din-{uuid.uuid4().hex[:6]}@example.com")
    token = await _mint_token(db, diner.email)
    r = await client.get("/api/v1/admin/deals/pending", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


# ---- Visibility ----
@pytest.mark.asyncio
async def test_approved_active_deal_appears_in_public_listing(client: AsyncClient, db):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    curator = await _make_curator(db, f"cur-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    curator_token = await _mint_token(db, curator.email)
    rid = await _make_restaurant(client, owner_token)

    # Create 3 deals: 1 draft, 1 approved, 1 rejected.
    body = _good_deal_body()
    drafts = []
    for title in ("Deal A draft", "Deal B approved", "Deal C rejected"):
        body2 = {**body, "title": title}
        rr = await client.post(
            f"/api/v1/restaurants/{rid}/deals",
            json=body2,
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        drafts.append(rr.json()["id"])

    # Submit B and approve; submit C and reject.
    await client.post(f"/api/v1/deals/{drafts[1]}/submit", headers={"Authorization": f"Bearer {owner_token}"})
    await client.post(
        f"/api/v1/admin/deals/{drafts[1]}/approve",
        headers={"Authorization": f"Bearer {curator_token}"},
    )
    await client.post(f"/api/v1/deals/{drafts[2]}/submit", headers={"Authorization": f"Bearer {owner_token}"})
    await client.post(
        f"/api/v1/admin/deals/{drafts[2]}/reject",
        json={"reason": "nope"},
        headers={"Authorization": f"Bearer {curator_token}"},
    )

    r = await client.get(f"/api/v1/restaurants/{rid}/deals/active")
    assert r.status_code == 200
    titles = [d["title"] for d in r.json()]
    assert titles == ["Deal B approved"]


@pytest.mark.asyncio
async def test_expired_deal_disappears_from_public_listing(client: AsyncClient, db):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    curator = await _make_curator(db, f"cur-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    curator_token = await _mint_token(db, curator.email)
    rid = await _make_restaurant(client, owner_token)

    # Create a deal that ends today (still APPROVED, not yet expired by job)
    r = await client.post(
        f"/api/v1/admin/deals/create",
        params={"restaurant_id": rid},
        json=_good_deal_body(
            title="Ends today",
            end_date=date.today().isoformat(),
        ),
        headers={"Authorization": f"Bearer {curator_token}"},
    )
    assert r.status_code == 201
    assert r.json()["status"] == DealStatus.APPROVED.value

    # Public listing includes it (end_date >= today)
    pub = await client.get(f"/api/v1/restaurants/{rid}/deals/active")
    assert len(pub.json()) == 1

    # Now backdate the deal (both dates) past today, and re-run the auto-expiry.
    d = await db.get(Deal, uuid.UUID(r.json()["id"]))
    d.start_date = date.today() - timedelta(days=10)
    d.end_date = date.today() - timedelta(days=1)
    await db.commit()
    n = await deal_service.expire_old_deals(db)
    assert n == 1

    # Public listing now empty
    pub2 = await client.get(f"/api/v1/restaurants/{rid}/deals/active")
    assert pub2.json() == []


@pytest.mark.asyncio
async def test_push_only_deal_gated_by_subscription(client: AsyncClient, db):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    curator = await _make_curator(db, f"cur-{uuid.uuid4().hex[:6]}@example.com")
    diner_subscribed = await _make_diner(db, f"sub-{uuid.uuid4().hex[:6]}@example.com")
    diner_unsubscribed = await _make_diner(db, f"unsub-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    curator_token = await _mint_token(db, curator.email)
    sub_token = await _mint_token(db, diner_subscribed.email)
    unsub_token = await _mint_token(db, diner_unsubscribed.email)
    # Premium-tier restaurant
    rid = await _make_restaurant(client, owner_token, tier=RestaurantTier.PREMIUM)

    # Seed the push subscription for one diner
    db.add(RestaurantSubscription(
        id=uuid.uuid4(), user_id=diner_subscribed.id, restaurant_id=uuid.UUID(rid),
    ))
    await db.commit()

    # Curator creates a PUSH_ONLY deal directly
    r = await client.post(
        f"/api/v1/admin/deals/create",
        params={"restaurant_id": rid},
        json=_good_deal_body(
            title="Push-only insider",
            target_audience=DealAudience.PUSH_ONLY.value,
        ),
        headers={"Authorization": f"Bearer {curator_token}"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["target_audience"] == DealAudience.PUSH_ONLY.value

    # Anonymous: doesn't see push-only
    anon = await client.get(f"/api/v1/restaurants/{rid}/deals/active")
    assert anon.json() == []

    # Subscribed diner: sees it
    sub = await client.get(
        f"/api/v1/restaurants/{rid}/deals/active",
        headers={"Authorization": f"Bearer {sub_token}"},
    )
    assert len(sub.json()) == 1
    assert sub.json()[0]["title"] == "Push-only insider"

    # Unsubscribed diner: still doesn't see it
    unsub = await client.get(
        f"/api/v1/restaurants/{rid}/deals/active",
        headers={"Authorization": f"Bearer {unsub_token}"},
    )
    assert unsub.json() == []


@pytest.mark.asyncio
async def test_push_only_blocked_for_non_premium_tier(client: AsyncClient, db):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    curator = await _make_curator(db, f"cur-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    curator_token = await _mint_token(db, curator.email)
    rid = await _make_restaurant(client, owner_token, tier=RestaurantTier.FREE)

    r = await client.post(
        f"/api/v1/admin/deals/create",
        params={"restaurant_id": rid},
        json=_good_deal_body(
            title="Push-only on free tier",
            target_audience=DealAudience.PUSH_ONLY.value,
        ),
        headers={"Authorization": f"Bearer {curator_token}"},
    )
    assert r.status_code == 400
    assert "premium" in r.json()["detail"].lower()
