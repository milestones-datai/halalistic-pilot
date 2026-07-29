"""Tests for Stage 8: points ledger, referrals, gift cards.

Coverage:
  - Append-only enforcement: no UPDATE/DELETE helpers in the service
    (verified by import + grep on the service module)
  - Balance reconciliation: cached value is re-synced from ledger on
    every read if drift is detected
  - Sign-up with ?ref=CODE attaches the referrer (good code, bad code,
    self-referral)
  - A trigger: admin verify-email fires referral credit when user has
    a referrer; no-op without a referrer
  - C trigger: off by default; when toggled on, first approved review
    fires referral credit
  - Checkin: 200 points, 1/day/restaurant cap (UNIQUE constraint → 409)
  - Redemption: threshold gate, ledger debit, pending_fulfillment row,
    admin fulfill/fail
  - Idempotency: re-crediting the same reference is a no-op
  - No third-party gift card provider call (verified by no
    `stripe`/`tremendous`/`tangocard` import in the gift_cards service)
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
import sqlalchemy as sa

from app.core.config import settings
from app.core.security import hash_password
from app.models.enums import (
    ReviewStatus,
    UserRole,
)
from app.models.points import Checkin, GiftCardRedemption, PointsTransaction
from app.models.review import Review
from app.models.user import User
from app.services import gift_cards as gift_cards_service
from app.services import points as points_service
from app.services import referrals as referrals_service


# ---- helpers ----
async def _make_user(db, email: str, role: UserRole = UserRole.DINER) -> User:
    u = User(
        id=uuid.uuid4(), email=email, password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=role,
    )
    db.add(u); await db.commit(); await db.refresh(u)
    return u


async def _make_owner(db, email: str) -> User:
    return await _make_user(db, email, UserRole.RESTAURANT_OWNER)


async def _make_admin(db, email: str) -> User:
    return await _make_user(db, email, UserRole.PLATFORM_ADMIN)


async def _mint_token(db, email: str) -> str:
    from app.services.auth_service import login as svc_login
    _, pair = await svc_login(db, email, "supersecret123")
    return pair.access_token


async def _make_restaurant(client, token: str) -> str:
    r = await client.post(
        "/api/v1/restaurants",
        json={"name": "X", "address_line": "1 St", "city": "Houston",
              "state": "TX", "postal_code": "77002", "country": "US",
              "price_range": "2", "cuisine_slugs": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---- Static: no UPDATE/DELETE helpers in the points service ----
def test_points_service_has_no_mutation_helpers():
    """The ledger is append-only. This test asserts the service module
    exposes no `update_transaction` / `delete_transaction` / `void_*`
    style helpers by simple attribute check.
    """
    forbidden = {"update_transaction", "delete_transaction", "void_transaction",
                 "edit_transaction", "remove_transaction"}
    exposed = set(dir(points_service))
    leaked = forbidden & exposed
    assert not leaked, f"points service exposes ledger-mutation helpers: {leaked}"


def test_gift_cards_service_has_no_provider_integration():
    """Per the prompt: NO third-party gift card provider integration in
    Stage 8. We check actual `import` and function-call lines, NOT the
    docstring (which names candidates for the founder to pick — that's
    the whole point of the TODO marker).
    """
    import re
    src = open(gift_cards_service.__file__, encoding="utf-8").read()
    # Strip docstrings (they may list candidate providers as TODOs).
    no_docstring_src = re.sub(r'"""[\s\S]*?"""', "", src)
    forbidden = ["import stripe", "from stripe", "tremendous", "tangocard", "rybbon"]
    for needle in forbidden:
        assert needle not in no_docstring_src.lower(), (
            f"gift_cards service references forbidden provider: {needle!r}"
        )


# ---- Balance reconciliation ----
@pytest.mark.asyncio
async def test_balance_matches_ledger_sum(db):
    """After a credit + a debit, get_balance() returns the sum of the
    ledger, and resyncs the cached user.points_balance if it drifted.
    """
    user = await _make_user(db, f"u-{uuid.uuid4().hex[:6]}@example.com")
    uid = user.id
    # 1. Credit referral
    await points_service.credit_for_referral(
        db, referrer_id=uid, referred_user_id=uuid.uuid4(),
    )
    assert await points_service.get_balance(db, uid) == 500  # config value

    # 2. Drift the cached value
    user.points_balance = 99999
    await db.commit()
    # 3. get_balance detects drift + re-syncs
    assert await points_service.get_balance(db, uid) == 500
    await db.refresh(user)
    assert user.points_balance == 500


@pytest.mark.asyncio
async def test_idempotent_credit(db):
    """Calling credit_for_referral twice for the same referred_user
    inserts one row, not two.
    """
    user = await _make_user(db, f"u-{uuid.uuid4().hex[:6]}@example.com")
    uid = user.id
    referred = uuid.uuid4()
    t1 = await points_service.credit_for_referral(db, referrer_id=uid, referred_user_id=referred)
    t2 = await points_service.credit_for_referral(db, referrer_id=uid, referred_user_id=referred)
    assert t1.id == t2.id, "duplicate credit returned a different row — should be the same one"
    rows = (await db.execute(
        select(PointsTransaction).where(
            PointsTransaction.user_id == uid, PointsTransaction.type == "referral",
        )
    )).scalars().all()
    assert len(rows) == 1


# ---- Sign-up with ref code ----
@pytest.mark.asyncio
async def test_signup_with_valid_ref_attaches_referrer(client: AsyncClient, db):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    code = await referrals_service.get_or_create_referral_code(db, owner)
    # A new diner signs up via the referrer's link.
    r = await client.post(
        "/api/v1/auth/register",
        params={"ref": code},
        json={"email": f"ref-{uuid.uuid4().hex[:6]}@example.com",
              "password": "supersecret123", "display_name": "Ref", "role": "diner"},
    )
    assert r.status_code == 201, r.text
    # Find the new user via /users/me (we don't have /me, but we can
    # look at the most recent diner). Use direct DB to be clean.
    new_user = (await db.execute(
        select(User).order_by(User.created_at.desc()).limit(1)
    )).scalar_one()
    assert new_user.referred_by_user_id == owner.id


@pytest.mark.asyncio
async def test_signup_with_bad_ref_does_not_crash(client: AsyncClient, db):
    r = await client.post(
        "/api/v1/auth/register",
        params={"ref": "ZZZZZZZZ"},
        json={"email": f"bad-{uuid.uuid4().hex[:6]}@example.com",
              "password": "supersecret123", "display_name": "Bad", "role": "diner"},
    )
    assert r.status_code == 201, "bad ref code should be ignored, not 4xx"


@pytest.mark.asyncio
async def test_self_referral_ignored(client: AsyncClient, db):
    """The attach_referrer service refuses to set referred_by when the
    new user IS the referrer. (In production this can never happen
    because a fresh signup always has a new UUID, but the guard exists
    as defense-in-depth.)
    """
    user = await _make_user(db, f"u-{uuid.uuid4().hex[:6]}@example.com")
    code = await referrals_service.get_or_create_referral_code(db, user)
    assert user.referral_code == code

    # Simulate: someone tries to "refer" themselves. The guard should
    # short-circuit before updating referred_by.
    pre = user.referred_by_user_id
    await referrals_service.attach_referrer_on_register(
        db, new_user=user, ref_code=code,
    )
    assert user.referred_by_user_id == pre, "self-referral was not blocked"


# ---- A trigger: admin verify-email ----
@pytest.mark.asyncio
async def test_admin_verify_email_fires_A_referral_credit(client: AsyncClient, db):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"adm-{uuid.uuid4().hex[:6]}@example.com")
    code = await referrals_service.get_or_create_referral_code(db, owner)
    # Sign up a new user via the ref link.
    new_email = f"ref-{uuid.uuid4().hex[:6]}@example.com"
    r = await client.post(
        "/api/v1/auth/register", params={"ref": code},
        json={"email": new_email, "password": "supersecret123",
              "display_name": "Ref", "role": "diner"},
    )
    assert r.status_code == 201
    new_user = (await db.execute(
        select(User).where(User.email == new_email)
    )).scalar_one()
    assert new_user.referred_by_user_id == owner.id
    # No credit yet (email not verified).
    assert await points_service.get_balance(db, owner.id) == 0
    # Admin verifies the email.
    admin_token = await _mint_token(db, admin.email)
    r = await client.post(
        f"/api/v1/admin/users/{new_user.id}/verify-email",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["referral_credited"] is True
    # Referrer now has 500 points.
    assert await points_service.get_balance(db, owner.id) == 500


@pytest.mark.asyncio
async def test_admin_verify_email_no_referrer_is_noop(client: AsyncClient, db):
    admin = await _make_admin(db, f"adm-{uuid.uuid4().hex[:6]}@example.com")
    user = await _make_user(db, f"u-{uuid.uuid4().hex[:6]}@example.com")
    admin_token = await _mint_token(db, admin.email)
    r = await client.post(
        f"/api/v1/admin/users/{user.id}/verify-email",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["referral_credited"] is False
    assert user.email_verified is True
    # No credit because no referrer.
    assert await points_service.get_balance(db, user.id) == 0


# ---- C trigger: first approved review ----
@pytest.mark.asyncio
async def test_C_trigger_off_by_default(client: AsyncClient, db, monkeypatch):
    """Even with an approved review, no referral credit fires while the
    C trigger is OFF (default)."""
    monkeypatch.setattr(settings, "points_referral_credit_on_first_review", False)
    referrer = await _make_owner(db, f"r-{uuid.uuid4().hex[:6]}@example.com")
    referred = await _make_user(db, f"d-{uuid.uuid4().hex[:6]}@example.com")
    referred.referred_by_user_id = referrer.id
    await db.commit()
    # Fire the C trigger directly. C is off, so no credit.
    credited = await referrals_service.credit_referral_if_eligible(db, referred_user_id=referred.id)
    assert credited is False
    assert await points_service.get_balance(db, referrer.id) == 0


@pytest.mark.asyncio
async def test_C_trigger_on_fires_on_first_approved_review(client: AsyncClient, db, monkeypatch):
    monkeypatch.setattr(settings, "points_referral_credit_on_first_review", True)
    referrer = await _make_owner(db, f"r-{uuid.uuid4().hex[:6]}@example.com")
    referred = await _make_user(db, f"d-{uuid.uuid4().hex[:6]}@example.com")
    referred.referred_by_user_id = referrer.id
    await db.commit()
    # Without an approved review, C is eligible on the flag but no review means no fire.
    credited = await referrals_service.credit_referral_if_eligible(db, referred_user_id=referred.id)
    assert credited is False
    # Insert an approved review for the referred user. The Review
    # table has a FK to restaurants, so we have to create a real
    # restaurant row first (use the test engine directly to skip the
    # full owner flow). Restaurant requires owner_id (NOT NULL).
    from tests.conftest import _test_engine
    from app.models.restaurant import Restaurant as RestModel
    rid_for_review = uuid.uuid4()
    async with _test_engine.begin() as conn:
        await conn.execute(
            sa.insert(RestModel).values(
                id=rid_for_review, owner_id=referred.id, name="r",
                slug=f"r-{rid_for_review.hex[:6]}",
                address_line="1 St", city="Houston", state="TX",
                postal_code="77002", country="US",
                price_range="2", tier="free",
            )
        )
    review = Review(
        id=uuid.uuid4(), restaurant_id=rid_for_review,
        reviewer_id=referred.id, rating=5, body="Great",
        moderation_status=ReviewStatus.APPROVED.value,
    )
    db.add(review); await db.commit()
    # Now C fires.
    credited = await referrals_service.credit_referral_if_eligible(db, referred_user_id=referred.id)
    assert credited is True
    assert await points_service.get_balance(db, referrer.id) == 500


@pytest.mark.asyncio
async def test_admin_toggle_C_via_endpoint(client: AsyncClient, db, monkeypatch):
    monkeypatch.setattr(settings, "points_referral_credit_on_first_review", False)
    admin = await _make_admin(db, f"adm-{uuid.uuid4().hex[:6]}@example.com")
    token = await _mint_token(db, admin.email)
    r = await client.patch(
        "/api/v1/admin/settings/points-referral-on-first-review",
        json={"enabled": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    assert settings.points_referral_credit_on_first_review is True


# ---- Checkin ----
@pytest.mark.asyncio
async def test_checkin_credits_200_points(client: AsyncClient, db):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_user(db, f"din-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    diner_token = await _mint_token(db, diner.email)
    rid = await _make_restaurant(client, owner_token)
    r = await client.post(
        "/api/v1/users/me/checkins",
        json={"restaurant_id": rid},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["points_awarded"] == 200
    assert await points_service.get_balance(db, diner.id) == 200


@pytest.mark.asyncio
async def test_checkin_same_day_same_restaurant_blocked(client: AsyncClient, db):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_user(db, f"din-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _mint_token(db, owner.email)
    diner_token = await _mint_token(db, diner.email)
    rid = await _make_restaurant(client, owner_token)
    r1 = await client.post(
        "/api/v1/users/me/checkins",
        json={"restaurant_id": rid},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/api/v1/users/me/checkins",
        json={"restaurant_id": rid},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r2.status_code == 409
    assert "already checked in" in r2.json()["detail"].lower()


# ---- Redemption ----
@pytest.mark.asyncio
async def test_redemption_below_threshold_rejected(client: AsyncClient, db):
    user = await _make_user(db, f"u-{uuid.uuid4().hex[:6]}@example.com")
    token = await _mint_token(db, user.email)
    r = await client.post(
        "/api/v1/users/me/gift-card-redemptions",
        json={"points_amount": 500},  # below 1000 threshold
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert "minimum" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_redemption_above_balance_rejected(client: AsyncClient, db):
    user = await _make_user(db, f"u-{uuid.uuid4().hex[:6]}@example.com")
    # Credit 1500 points.
    await points_service._record_transaction(
        db, user_id=user.id, type_="checkin", amount=1500,
        reference_id=uuid.uuid4(), note="seed",
    )
    token = await _mint_token(db, user.email)
    r = await client.post(
        "/api/v1/users/me/gift-card-redemptions",
        json={"points_amount": 5000},  # above balance
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert "insufficient" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_redemption_creates_pending_fulfillment_and_debits_ledger(
    client: AsyncClient, db
):
    user = await _make_user(db, f"u-{uuid.uuid4().hex[:6]}@example.com")
    # Credit 1500 points.
    await points_service._record_transaction(
        db, user_id=user.id, type_="checkin", amount=1500,
        reference_id=uuid.uuid4(), note="seed",
    )
    token = await _mint_token(db, user.email)
    r = await client.post(
        "/api/v1/users/me/gift-card-redemptions",
        json={"points_amount": 1000},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending_fulfillment"
    assert body["points_spent"] == 1000
    assert body["external_ref"] is None
    assert body["fulfilled_at"] is None
    # Balance is 500 now.
    assert await points_service.get_balance(db, user.id) == 500
    # The ledger has a negative row.
    debit = (await db.execute(
        select(PointsTransaction).where(
            PointsTransaction.user_id == user.id,
            PointsTransaction.type == "redemption",
        )
    )).scalar_one()
    assert debit.amount == -1000


@pytest.mark.asyncio
async def test_admin_fulfill_marks_fulfilled(client: AsyncClient, db):
    user = await _make_user(db, f"u-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"adm-{uuid.uuid4().hex[:6]}@example.com")
    await points_service._record_transaction(
        db, user_id=user.id, type_="checkin", amount=1500,
        reference_id=uuid.uuid4(), note="seed",
    )
    user_token = await _mint_token(db, user.email)
    r = await client.post(
        "/api/v1/users/me/gift-card-redemptions",
        json={"points_amount": 1000},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    rid = r.json()["id"]
    admin_token = await _mint_token(db, admin.email)
    f = await client.post(
        f"/api/v1/admin/gift-card-redemptions/{rid}/fulfill",
        json={"external_ref": "GIFT-CARD-ABC123", "note": "sent via email"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert f.status_code == 200, f.text
    body = f.json()
    assert body["status"] == "fulfilled"
    assert body["external_ref"] == "GIFT-CARD-ABC123"
    assert body["fulfillment_note"] == "sent via email"
    assert body["fulfilled_at"] is not None


@pytest.mark.asyncio
async def test_admin_cannot_re_fulfill_fulfilled(client: AsyncClient, db):
    user = await _make_user(db, f"u-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"adm-{uuid.uuid4().hex[:6]}@example.com")
    await points_service._record_transaction(
        db, user_id=user.id, type_="checkin", amount=1500,
        reference_id=uuid.uuid4(), note="seed",
    )
    user_token = await _mint_token(db, user.email)
    r = await client.post(
        "/api/v1/users/me/gift-card-redemptions",
        json={"points_amount": 1000},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    rid = r.json()["id"]
    admin_token = await _mint_token(db, admin.email)
    await client.post(
        f"/api/v1/admin/gift-card-redemptions/{rid}/fulfill",
        json={"external_ref": "G1"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    r2 = await client.post(
        f"/api/v1/admin/gift-card-redemptions/{rid}/fulfill",
        json={"external_ref": "G2"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_admin_fail_marks_failed(client: AsyncClient, db):
    user = await _make_user(db, f"u-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"adm-{uuid.uuid4().hex[:6]}@example.com")
    await points_service._record_transaction(
        db, user_id=user.id, type_="checkin", amount=1500,
        reference_id=uuid.uuid4(), note="seed",
    )
    user_token = await _mint_token(db, user.email)
    r = await client.post(
        "/api/v1/users/me/gift-card-redemptions",
        json={"points_amount": 1000},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    rid = r.json()["id"]
    admin_token = await _mint_token(db, admin.email)
    f = await client.post(
        f"/api/v1/admin/gift-card-redemptions/{rid}/fail",
        json={"reason": "test fail"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert f.status_code == 200
    assert f.json()["status"] == "failed"
    # NOTE: the points are NOT auto-refunded in MVP. That's a known
    # Phase 2 enhancement (see the docstring in gift_cards.fail).
    # The user can still see the failed row.
    assert await points_service.get_balance(db, user.id) == 500
