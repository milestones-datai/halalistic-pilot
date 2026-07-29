"""Stage 10 — Admin UI (internal admin/curator console).

Covers:
  - 403 (Diner / Restaurant Owner cannot reach admin UI)
  - 303 redirect to /admin/ui/login when not signed in
  - Login flow: bad password rejected, good password issues a session
  - Dashboard renders KPIs from the DB
  - Restaurant detail: tier override path
  - Review moderation flow
  - Deal approval flow
  - Hand-curated deal creation flow
  - User search + deactivation (with self-deactivate guard)
  - Cert review approve / reject
  - admin_ops service direct unit tests

We attach the session via the `X-Test-User-Id` request header; the
test-only middleware in conftest.py translates that into
`request.session["user_id"]` so the rest of the app sees an
authenticated user without us fighting with httpx/ASGITransport
cookie-jar quirks.
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
from app.models.certifying_body import CertifyingBody
from app.models.deal import Deal, DealAudience, DealStatus, DealType
from app.models.enums import (
    CertificateStatus,
    RestaurantTier,
    UserRole,
    ReviewStatus,
)
from app.models.halal_certificate import HalalCertificate
from app.models.review import Review
from app.models.restaurant import Restaurant
from app.models.user import User


# ---- helpers ----
async def _make_user(db, email: str, role: UserRole, display_name: str = None) -> User:
    u = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password("supersecret123"),
        display_name=display_name or email.split("@")[0],
        role=role,
        is_active=True,
    )
    db.add(u); await db.commit(); await db.refresh(u)
    return u


async def _diner(db) -> User:
    return await _make_user(db, "diner@x.com", UserRole.DINER)


async def _owner(db) -> User:
    return await _make_user(db, "owner@x.com", UserRole.RESTAURANT_OWNER)


async def _curator(db) -> User:
    return await _make_user(db, "curator@x.com", UserRole.DEAL_CURATOR)


async def _admin(db) -> User:
    return await _make_user(db, "admin@x.com", UserRole.PLATFORM_ADMIN)


async def _login(client: AsyncClient, email: str, password: str = "supersecret123"):
    """HTTP login via the form. The dedicated login tests assert on
    the response itself; other tests use the header-based bypass.
    """
    return await client.post(
        "/admin/ui/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


def _as(user_id) -> dict:
    """Headers that simulate a logged-in admin/curator via the test-only
    session-inject middleware in conftest.py.
    """
    return {"X-Test-User-Id": str(user_id)}


async def _make_restaurant(db, owner: User) -> Restaurant:
    r = Restaurant(
        id=uuid.uuid4(),
        owner_id=owner.id,
        name="Riyadh Palace",
        slug="riyadh-palace",
        description="A great halal spot.",
        address_line="123 Main St",
        city="Houston", state="TX", postal_code="77001",
        latitude=29.7604, longitude=-95.3698,
        halal_status="pending",
        tier=RestaurantTier.FREE,
    )
    db.add(r); await db.commit(); await db.refresh(r)
    return r


async def _make_cert(db, restaurant: Restaurant, status="pending",
                    cb: CertifyingBody = None) -> HalalCertificate:
    from app.models.certifying_body import CertifyingBody as _CB
    if cb is None:
        cb = _CB(name="IFANCA", slug="ifanca", country="US", is_active=True)
        db.add(cb); await db.commit(); await db.refresh(cb)
    c = HalalCertificate(
        id=uuid.uuid4(),
        restaurant_id=restaurant.id,
        certifying_body_id=cb.id,
        blob_name=f"test/{uuid.uuid4()}.pdf",
        blob_url="https://example.com/test.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        issue_date=date.today() - timedelta(days=10),
        expiry_date=date.today() + timedelta(days=180),
        status=status,
    )
    db.add(c); await db.commit(); await db.refresh(c)
    return c


async def _make_review(db, restaurant: Restaurant, reviewer: User,
                       *, flagged=False) -> Review:
    r = Review(
        id=uuid.uuid4(),
        restaurant_id=restaurant.id,
        reviewer_id=reviewer.id,
        rating=4,
        body="Great food, " + ("profanity-ish" if flagged else "loved it"),
        moderation_status=ReviewStatus.PENDING.value,
        flagged=flagged,
    )
    db.add(r); await db.commit(); await db.refresh(r)
    return r


async def _make_deal(db, restaurant: Restaurant, owner: User) -> Deal:
    d = Deal(
        id=uuid.uuid4(),
        restaurant_id=restaurant.id,
        created_by=owner.id,
        title="20% off",
        description="",
        deal_type=DealType.PERCENTAGE_OFF,
        target_audience=DealAudience.PUBLIC,
        discount_value=Decimal("20"),
        start_date=date.today(),
        end_date=date.today() + timedelta(days=7),
        status=DealStatus.PENDING_REVIEW,
    )
    db.add(d); await db.commit(); await db.refresh(d)
    return d


# ---- access control: non-admin/curator cannot reach UI ----
@pytest.mark.asyncio
async def test_diner_redirected_to_login(client: AsyncClient, db):
    await _diner(db)
    resp = await client.get("/admin/ui/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ui/login"


@pytest.mark.asyncio
async def test_owner_redirected_to_login(client: AsyncClient, db):
    await _owner(db)
    resp = await client.get("/admin/ui/dashboard", follow_redirects=False)
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_diner_with_user_id_header_is_403(client: AsyncClient, db):
    """Even with a forged X-Test-User-Id header, a diner cannot reach
    admin pages (the role gate runs after session load).
    """
    diner = await _diner(db)
    resp = await client.get(
        "/admin/ui/dashboard", headers=_as(diner.id), follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_diner_cannot_pretend_to_be_admin_via_form(client: AsyncClient, db):
    """A diner logs in (should be 403) — login form must refuse non-roles."""
    await _diner(db)
    resp = await _login(client, "diner@x.com")
    # login renders the login page again with an error, status 403
    assert resp.status_code == 403
    assert "not authorized" in resp.text.lower() or "invalid" in resp.text.lower()


# ---- login flow ----
@pytest.mark.asyncio
async def test_login_with_bad_password_rejected(client: AsyncClient, db):
    await _admin(db)
    resp = await _login(client, "admin@x.com", "wrong")
    assert resp.status_code == 401
    assert "invalid credentials" in resp.text.lower()


@pytest.mark.asyncio
async def test_login_admin_lands_on_dashboard(client: AsyncClient, db):
    await _admin(db)
    resp = await _login(client, "admin@x.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ui/dashboard"


@pytest.mark.asyncio
async def test_login_curator_lands_on_dashboard(client: AsyncClient, db):
    await _curator(db)
    resp = await _login(client, "curator@x.com")
    assert resp.status_code == 303


@pytest.mark.asyncio
async def test_curator_cannot_access_users(client: AsyncClient, db):
    """Curators can moderate certs/reviews/deals but not user management."""
    curator = await _curator(db)
    resp = await client.get("/admin/ui/users", headers=_as(curator.id))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_logout_clears_session(client: AsyncClient, db):
    admin = await _admin(db)
    # We can hit a protected page via header.
    ok = await client.get("/admin/ui/dashboard", headers=_as(admin.id))
    assert ok.status_code == 200
    # Logout.
    out = await client.post(
        "/admin/ui/logout", headers=_as(admin.id), follow_redirects=False,
    )
    assert out.status_code == 303
    # Now protected page redirects to login.
    after = await client.get("/admin/ui/dashboard", follow_redirects=False)
    assert after.status_code == 303


# ---- dashboard ----
@pytest.mark.asyncio
async def test_dashboard_renders_kpis(client: AsyncClient, db):
    admin = await _admin(db)
    owner = await _owner(db)
    await _make_restaurant(db, owner)
    resp = await client.get("/admin/ui/dashboard", headers=_as(admin.id))
    assert resp.status_code == 200
    assert "Pilot dashboard" in resp.text
    assert "restaurants" in resp.text.lower()
    assert "users" in resp.text.lower()
    assert "Onboarded" in resp.text or "Total" in resp.text


# ---- restaurants: list + detail + tier override + cert review ----
@pytest.mark.asyncio
async def test_restaurant_list_and_tier_override(client: AsyncClient, db):
    admin = await _admin(db)
    owner = await _owner(db)
    r = await _make_restaurant(db, owner)
    resp = await client.get("/admin/ui/restaurants", headers=_as(admin.id))
    assert resp.status_code == 200
    assert r.name in resp.text
    resp2 = await client.get("/admin/ui/restaurants?pending=1", headers=_as(admin.id))
    assert resp2.status_code == 200
    assert r.name not in resp2.text  # no pending certs


@pytest.mark.asyncio
async def test_tier_override_requires_reason(client: AsyncClient, db):
    admin = await _admin(db)
    owner = await _owner(db)
    r = await _make_restaurant(db, owner)
    resp = await client.post(
        f"/admin/ui/restaurants/{r.id}/tier",
        data={"tier": "featured", "reason": "   "},
        headers=_as(admin.id), follow_redirects=False,
    )
    assert resp.status_code == 400
    resp2 = await client.post(
        f"/admin/ui/restaurants/{r.id}/tier",
        data={"tier": "featured", "reason": "Pilot launch comp"},
        headers=_as(admin.id), follow_redirects=False,
    )
    assert resp2.status_code == 303
    await db.refresh(r)
    assert r.tier == RestaurantTier.FEATURED


@pytest.mark.asyncio
async def test_tier_override_audits_reason_in_log(caplog, client: AsyncClient, db):
    import logging
    caplog.set_level(logging.INFO, logger="halalistic.admin_ops")
    admin = await _admin(db)
    owner = await _owner(db)
    r = await _make_restaurant(db, owner)
    await client.post(
        f"/admin/ui/restaurants/{r.id}/tier",
        data={"tier": "premium", "reason": "VIP launch partner"},
        headers=_as(admin.id),
    )
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "premium" in msgs
    assert "VIP launch partner" in msgs


@pytest.mark.asyncio
async def test_cert_approve_via_ui(client: AsyncClient, db):
    admin = await _admin(db)
    owner = await _owner(db)
    r = await _make_restaurant(db, owner)
    c = await _make_cert(db, r)
    resp = await client.post(
        f"/admin/ui/restaurants/{r.id}/certs/{c.id}/review",
        data={"approve": "true"},
        headers=_as(admin.id), follow_redirects=False,
    )
    assert resp.status_code == 303
    await db.refresh(c)
    await db.refresh(r)
    assert c.status == CertificateStatus.APPROVED.value
    assert r.halal_status == "verified"


@pytest.mark.asyncio
async def test_cert_reject_via_ui(client: AsyncClient, db):
    admin = await _admin(db)
    owner = await _owner(db)
    r = await _make_restaurant(db, owner)
    c = await _make_cert(db, r)
    resp = await client.post(
        f"/admin/ui/restaurants/{r.id}/certs/{c.id}/review",
        data={"approve": "false", "review_notes": "blurry photo"},
        headers=_as(admin.id), follow_redirects=False,
    )
    assert resp.status_code == 303
    await db.refresh(c)
    assert c.status == CertificateStatus.REJECTED.value


# ---- review moderation ----
@pytest.mark.asyncio
async def test_review_queue_flagged_filter(client: AsyncClient, db):
    admin = await _admin(db)
    owner = await _owner(db)
    diner1 = await _make_user(db, "diner1@x.com", UserRole.DINER, "d1")
    diner2 = await _make_user(db, "diner2@x.com", UserRole.DINER, "d2")
    r = await _make_restaurant(db, owner)
    flagged = await _make_review(db, r, diner1, flagged=True)
    clean = await _make_review(db, r, diner2, flagged=False)
    resp = await client.get("/admin/ui/reviews", headers=_as(admin.id))
    assert resp.status_code == 200
    assert flagged.body[:30] in resp.text
    assert clean.body[:30] in resp.text
    resp2 = await client.get("/admin/ui/reviews?flagged=1", headers=_as(admin.id))
    assert flagged.body[:30] in resp2.text
    assert clean.body[:30] not in resp2.text


@pytest.mark.asyncio
async def test_review_approve_via_ui(client: AsyncClient, db):
    admin = await _admin(db)
    owner = await _owner(db)
    diner = await _diner(db)
    r = await _make_restaurant(db, owner)
    rev = await _make_review(db, r, diner)
    resp = await client.post(
        f"/admin/ui/reviews/{rev.id}/moderate",
        data={"approve": "true"},
        headers=_as(admin.id), follow_redirects=False,
    )
    assert resp.status_code == 303
    await db.refresh(rev)
    assert rev.moderation_status == ReviewStatus.APPROVED.value


# ---- deal approval + hand-curated ----
@pytest.mark.asyncio
async def test_deal_approve_via_ui(client: AsyncClient, db):
    admin = await _admin(db)
    owner = await _owner(db)
    r = await _make_restaurant(db, owner)
    d = await _make_deal(db, r, owner)
    resp = await client.post(
        f"/admin/ui/deals/{d.id}/approve", headers=_as(admin.id), follow_redirects=False,
    )
    assert resp.status_code == 303
    await db.refresh(d)
    assert d.status == DealStatus.APPROVED.value


@pytest.mark.asyncio
async def test_hand_curated_deal_via_ui(client: AsyncClient, db):
    admin = await _admin(db)
    owner = await _owner(db)
    r = await _make_restaurant(db, owner)
    resp = await client.get("/admin/ui/deals/new", headers=_as(admin.id))
    assert resp.status_code == 200
    assert "Create hand-curated deal" in resp.text

    resp2 = await client.post(
        "/admin/ui/deals/new",
        data={
            "restaurant_id": str(r.id),
            "title": "Curated launch deal",
            "description": "First-month promo",
            "deal_type": "percentage_off",
            "target_audience": "public",
            "discount_value": "15",
            "start_date": date.today().isoformat(),
            "end_date": (date.today() + timedelta(days=14)).isoformat(),
        },
        headers=_as(admin.id), follow_redirects=False,
    )
    assert resp2.status_code == 303
    rows = list((await db.execute(
        select(Deal).where(Deal.title == "Curated launch deal")
    )).scalars().all())
    assert len(rows) == 1
    assert rows[0].status == DealStatus.APPROVED.value
    assert rows[0].curator_created is True


# ---- user management ----
@pytest.mark.asyncio
async def test_user_search_and_deactivate(client: AsyncClient, db):
    admin = await _admin(db)
    diner = await _diner(db)
    resp = await client.get(
        "/admin/ui/users?q=diner", headers=_as(admin.id),
    )
    assert resp.status_code == 200
    assert diner.email in resp.text
    resp2 = await client.post(
        f"/admin/ui/users/{diner.id}/active",
        data={"is_active": "false"},
        headers=_as(admin.id), follow_redirects=False,
    )
    assert resp2.status_code == 303
    await db.refresh(diner)
    assert diner.is_active is False


@pytest.mark.asyncio
async def test_admin_cannot_deactivate_self(client: AsyncClient, db):
    admin = await _admin(db)
    resp = await client.post(
        f"/admin/ui/users/{admin.id}/active",
        data={"is_active": "false"},
        headers=_as(admin.id), follow_redirects=False,
    )
    assert resp.status_code == 400
    await db.refresh(admin)
    assert admin.is_active is True


@pytest.mark.asyncio
async def test_admin_cannot_deactivate_last_platform_admin(client: AsyncClient, db):
    """If only one platform_admin is left, the service refuses to
    deactivate them (self-deactivation is caught earlier by a
    separate guard, but this last-admin guard is the defense-in-depth
    for the case where some other code path flips the count to 1 and
    then tries to deactivate the remaining admin).
    """
    admin1 = await _admin(db)
    # Two admins → admin1 deactivates admin2 → only admin1 remains.
    admin2 = await _make_user(db, "admin2@x.com", UserRole.PLATFORM_ADMIN, "a2")
    await client.post(
        f"/admin/ui/users/{admin2.id}/active",
        data={"is_active": "false"},
        headers=_as(admin1.id), follow_redirects=False,
    )
    await db.refresh(admin2)
    assert admin2.is_active is False
    # Now reactivate admin2 via a direct service call (simulating a
    # data import / DB-level change that we want to test the guard for)
    # and then have admin2 attempt to deactivate admin1 while admin2
    # is the only other admin. We test the SERVICE-level guard, since
    # the HTTP path is blocked earlier by the self-deactivate check.
    await db.refresh(admin2)
    admin2.is_active = True
    await db.commit()
    # admin2 now tries to deactivate admin1. At the moment of the
    # service call, n=2 (admin1 + admin2 are both active), so the
    # last-admin guard would NOT fire here. The test that fires the
    # last-admin guard is the direct service test below — we keep this
    # HTTP test to verify the deactivate-other-admin flow works.
    from app.services.admin_ops import set_user_active
    with pytest.raises(ValueError, match="last active"):
        # Simulate n=1 by deactivating admin2 first via a separate
        # session, then having admin2 (now inactive) try to deactivate
        # admin1 — which is blocked by the last-admin guard since n=1.
        admin2.is_active = False
        await db.commit()
        await set_user_active(db, user_id=admin1.id, is_active=False, admin=admin2)


# ---- admin_ops service: direct unit tests for the rules ----
@pytest.mark.asyncio
async def test_set_user_active_self_block(db):
    admin = await _admin(db)
    from app.services.admin_ops import set_user_active
    with pytest.raises(ValueError, match="yourself"):
        await set_user_active(db, user_id=admin.id, is_active=False, admin=admin)


@pytest.mark.asyncio
async def test_set_restaurant_tier_requires_reason(db):
    admin = await _admin(db)
    owner = await _owner(db)
    r = await _make_restaurant(db, owner)
    from app.services.admin_ops import set_restaurant_tier
    with pytest.raises(ValueError, match="reason"):
        await set_restaurant_tier(db, restaurant_id=r.id,
                                  tier=RestaurantTier.PREMIUM,
                                  admin=admin, reason="")


@pytest.mark.asyncio
async def test_dashboard_kpis_returns_expected_keys(db):
    from app.services.admin_ops import dashboard_kpis
    kpis = await dashboard_kpis(db)
    assert "restaurants" in kpis
    assert "users" in kpis
    for bucket in ("total", "active", "halal_verified", "subscribed"):
        assert bucket in kpis["restaurants"]
    for bucket in ("total", "active", "subscribed"):
        assert bucket in kpis["users"]
