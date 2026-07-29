"""CRUD + ownership RBAC + admin override for restaurants."""
import io
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select

from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.user import User


async def _make_user(client: AsyncClient, db, email: str, role: UserRole) -> tuple[dict, User]:
    """Create a user + return (access_token, user_record).

    For non-self-signup roles (admin/curator), we insert the user directly in
    the SAME session the test client uses (so login can find them).
    """
    if role in (UserRole.DINER, UserRole.RESTAURANT_OWNER):
        resp = await client.post("/api/v1/auth/register", json={
            "email": email, "password": "supersecret123",
            "display_name": email.split("@")[0], "role": role.value,
        })
        assert resp.status_code == 201, resp.text
        return resp.json(), None  # DB record not available via API

    # Admin / curator: insert via the test's `db` session so login can see it.
    u = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0],
        role=role,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    return r.json(), u


async def _make_png_bytes(width: int = 50, height: int = 50) -> bytes:
    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_file(name: str = "p.png") -> tuple:
    """Return a (filename, file_obj, content_type) tuple for httpx files= param."""
    img = Image.new("RGB", (50, 50), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return (name, buf, "image/png")


def _restaurant_payload(name: str = "Karachi Kebab House") -> dict:
    return {
        "name": name,
        "description": "Best kebabs in Houston, zabiha halal only.",
        "address_line": "123 Main St",
        "city": "Houston",
        "state": "TX",
        "postal_code": "77002",
        "country": "US",
        "price_range": "2",
        "phone": "+1-555-0100",
        "cuisine_slugs": [],
    }


# ---- Create ----
@pytest.mark.asyncio
async def test_owner_can_create_restaurant(client: AsyncClient, db):
    """A Restaurant Owner can create a restaurant listing."""
    body, _ = await _make_user(client, db, f"owner-{uuid.uuid4().hex[:6]}@example.com", UserRole.RESTAURANT_OWNER)
    token = body["access_token"]
    resp = await client.post(
        "/api/v1/restaurants",
        json=_restaurant_payload(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "Karachi Kebab House"
    assert data["tier"] == "free"
    assert data["halal_status"] == "unverified"
    assert data["photo_cap"] == 2  # free tier cap


@pytest.mark.asyncio
async def test_diner_cannot_create_restaurant(client: AsyncClient, db):
    """Only Restaurant Owners (or Admins) can create restaurants — 403 for Diner."""
    body, _ = await _make_user(client, db, f"diner-{uuid.uuid4().hex[:6]}@example.com", UserRole.DINER)
    token = body["access_token"]
    resp = await client.post(
        "/api/v1/restaurants",
        json=_restaurant_payload(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ---- Update / ownership check ----
@pytest.mark.asyncio
async def test_owner_can_update_own_restaurant(client: AsyncClient, db):
    body, _ = await _make_user(client, db, f"owner1-{uuid.uuid4().hex[:6]}@example.com", UserRole.RESTAURANT_OWNER)
    token = body["access_token"]
    r = await client.post(
        "/api/v1/restaurants",
        json=_restaurant_payload("Update Me"),
        headers={"Authorization": f"Bearer {token}"},
    )
    rid = r.json()["id"]
    r2 = await client.put(
        f"/api/v1/restaurants/{rid}",
        json={"description": "Now with longer hours"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["description"] == "Now with longer hours"


@pytest.mark.asyncio
async def test_owner_cannot_update_other_owners_restaurant(client: AsyncClient, db):
    """The DoD-critical case: Owner A cannot edit Owner B's restaurant."""
    a_body, _ = await _make_user(client, db, f"a-{uuid.uuid4().hex[:6]}@example.com", UserRole.RESTAURANT_OWNER)
    b_body, _ = await _make_user(client, db, f"b-{uuid.uuid4().hex[:6]}@example.com", UserRole.RESTAURANT_OWNER)
    a_token = a_body["access_token"]
    b_token = b_body["access_token"]
    # Owner A creates
    r = await client.post(
        "/api/v1/restaurants",
        json=_restaurant_payload("A's place"),
        headers={"Authorization": f"Bearer {a_token}"},
    )
    rid = r.json()["id"]
    # Owner B tries to edit — must be 403
    r2 = await client.put(
        f"/api/v1/restaurants/{rid}",
        json={"description": "B hijacking A's restaurant"},
        headers={"Authorization": f"Bearer {b_token}"},
    )
    assert r2.status_code == 403, f"expected 403, got {r2.status_code}: {r2.text}"


@pytest.mark.asyncio
async def test_admin_can_edit_any_restaurant(client: AsyncClient, db):
    a_body, _ = await _make_user(client, db, f"owner-{uuid.uuid4().hex[:6]}@example.com", UserRole.RESTAURANT_OWNER)
    admin_body, _ = await _make_user(client, db, f"root-{uuid.uuid4().hex[:6]}@example.com", UserRole.PLATFORM_ADMIN)
    a_token = a_body["access_token"]
    admin_token = admin_body["access_token"]
    r = await client.post(
        "/api/v1/restaurants",
        json=_restaurant_payload("Owner's place"),
        headers={"Authorization": f"Bearer {a_token}"},
    )
    rid = r.json()["id"]
    r2 = await client.put(
        f"/api/v1/restaurants/{rid}",
        json={"description": "Admin override"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["description"] == "Admin override"


# ---- Photo upload + tier cap ----
@pytest.mark.asyncio
async def test_photo_cap_free_tier_allows_2_blocks_3rd(client: AsyncClient, db):
    body, _ = await _make_user(client, db, f"owner-photo-{uuid.uuid4().hex[:6]}@example.com", UserRole.RESTAURANT_OWNER)
    token = body["access_token"]
    r = await client.post(
        "/api/v1/restaurants",
        json=_restaurant_payload("Photo test"),
        headers={"Authorization": f"Bearer {token}"},
    )
    rid = r.json()["id"]

    # 1st: ok
    r1 = await client.post(
        f"/api/v1/restaurants/{rid}/photos",
        files={"file": _png_file("p1.png")},
        data={"caption": "first"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 201, r1.text

    # 2nd: ok (free tier cap = 2)
    r2 = await client.post(
        f"/api/v1/restaurants/{rid}/photos",
        files={"file": _png_file("p2.png")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 201, r2.text

    # 3rd: 409 photo_cap_exceeded
    r3 = await client.post(
        f"/api/v1/restaurants/{rid}/photos",
        files={"file": _png_file("p3.png")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r3.status_code == 409, r3.text
    body3 = r3.json()["detail"]
    assert body3["error"] == "photo_cap_exceeded"
    assert body3["cap"] == 2
    assert body3["current"] == 2


@pytest.mark.asyncio
async def test_photo_cap_premium_tier_allows_3(client: AsyncClient, db):
    """Premium tier cap is 10 — three uploads should all succeed."""
    owner_body, _ = await _make_user(client, db, f"prem-owner-{uuid.uuid4().hex[:6]}@example.com", UserRole.RESTAURANT_OWNER)
    admin_body, _ = await _make_user(client, db, f"prem-root-{uuid.uuid4().hex[:6]}@example.com", UserRole.PLATFORM_ADMIN)
    owner_token = owner_body["access_token"]
    admin_token = admin_body["access_token"]
    r = await client.post(
        "/api/v1/restaurants",
        json=_restaurant_payload("Premium test"),
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    rid = r.json()["id"]
    # Admin promotes to premium
    r_t = await client.put(
        f"/api/v1/admin/restaurants/{rid}/tier",
        json={"tier": "premium"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r_t.status_code == 200
    for i in range(3):
        ri = await client.post(
            f"/api/v1/restaurants/{rid}/photos",
            files={"file": _png_file(f"p{i}.png")},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert ri.status_code == 201, f"upload {i+1} failed: {ri.status_code} {ri.text}"


@pytest.mark.asyncio
async def test_other_owner_cannot_upload_photos(client: AsyncClient, db):
    a_body, _ = await _make_user(client, db, f"a-{uuid.uuid4().hex[:6]}@example.com", UserRole.RESTAURANT_OWNER)
    b_body, _ = await _make_user(client, db, f"b-{uuid.uuid4().hex[:6]}@example.com", UserRole.RESTAURANT_OWNER)
    a_token = a_body["access_token"]
    b_token = b_body["access_token"]
    r = await client.post(
        "/api/v1/restaurants",
        json=_restaurant_payload("A's photos"),
        headers={"Authorization": f"Bearer {a_token}"},
    )
    rid = r.json()["id"]
    r2 = await client.post(
        f"/api/v1/restaurants/{rid}/photos",
        files={"file": _png_file("p.png")},
        headers={"Authorization": f"Bearer {b_token}"},
    )
    assert r2.status_code == 403


# ---- Public GET profile ----
@pytest.mark.asyncio
async def test_public_profile_returns_shape(client: AsyncClient, db):
    body, _ = await _make_user(client, db, f"owner-{uuid.uuid4().hex[:6]}@example.com", UserRole.RESTAURANT_OWNER)
    token = body["access_token"]
    r = await client.post(
        "/api/v1/restaurants",
        json=_restaurant_payload("Profile test"),
        headers={"Authorization": f"Bearer {token}"},
    )
    rid = r.json()["id"]

    # No auth header — public endpoint
    r2 = await client.get(f"/api/v1/restaurants/{rid}")
    assert r2.status_code == 200, r2.text
    data = r2.json()
    assert data["restaurant"]["name"] == "Profile test"
    assert data["cuisines"] == []
    assert data["photos"] == []
    assert data["halal_badge"]["status"] == "unverified"
    assert data["aggregate_rating"] is None  # stubbed, Stage 5
    assert data["active_deals"] == []  # stubbed, Stage 6
    assert "menu" in data  # menu key exists (may be empty list)
