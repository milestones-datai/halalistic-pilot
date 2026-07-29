"""RBAC: role-dependency enforcement. Diner hitting Admin endpoint must get 403."""
import uuid
import pytest
from httpx import AsyncClient


async def _register_diner(client: AsyncClient, email: str) -> dict:
    r = await client.post("/api/v1/auth/register", json={
        "email": email, "password": "supersecret123", "display_name": "U", "role": "diner",
    })
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_diner_cannot_hit_admin_endpoint(client: AsyncClient):
    """The DoD-checkable case: Diner token on Admin endpoint MUST be 403, not 500."""
    body = await _register_diner(client, "diner1@example.com")
    token = body["access_token"]
    target_uid = "00000000-0000-0000-0000-000000000001"
    resp = await client.post(
        f"/api/v1/admin/users/{target_uid}/role",
        json={"role": "deal_curator"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_no_token_on_admin_endpoint_is_401(client: AsyncClient):
    resp = await client.post(
        "/api/v1/admin/users/00000000-0000-0000-0000-000000000002/role",
        json={"role": "deal_curator"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_promotes_user_to_curator(client: AsyncClient, db):
    """Happy path: Admin promotes a Diner to Deal Curator via the admin endpoint."""
    # Create a diner via the API (so the test exercises the full path).
    await _register_diner(client, "subject@example.com")

    # Create a Platform Admin directly via the DB (no self-signup path, by design).
    from app.core.security import hash_password
    from app.models.enums import UserRole
    from app.models.user import User
    admin = User(
        id=uuid.uuid4(),
        email="root@example.com",
        password_hash=hash_password("supersecret123"),
        display_name="Root",
        role=UserRole.PLATFORM_ADMIN,
    )
    db.add(admin)
    await db.commit()

    # Log in as the admin.
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "root@example.com", "password": "supersecret123"},
    )
    assert r.status_code == 200
    admin_token = r.json()["access_token"]

    # Find the diner's id.
    from sqlalchemy import select
    res = await db.execute(select(User).where(User.email == "subject@example.com"))
    subject = res.scalar_one()
    subject_id = str(subject.id)

    # Promote.
    r2 = await client.post(
        f"/api/v1/admin/users/{subject_id}/role",
        json={"role": "deal_curator"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["role"] == "deal_curator"


@pytest.mark.asyncio
async def test_admin_cannot_assign_diner_or_restaurant_owner(client: AsyncClient, db):
    """admin_assignable() should reject diner/restaurant_owner — only curator/admin allowed."""
    from app.core.security import hash_password
    from app.models.enums import UserRole
    from app.models.user import User
    admin = User(
        id=uuid.uuid4(),
        email="root3@example.com",
        password_hash=hash_password("supersecret123"),
        display_name="Root",
        role=UserRole.PLATFORM_ADMIN,
    )
    db.add(admin)
    await db.commit()
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "root3@example.com", "password": "supersecret123"},
    )
    admin_token = r.json()["access_token"]
    for bad_role in ("diner", "restaurant_owner"):
        r2 = await client.post(
            "/api/v1/admin/users/00000000-0000-0000-0000-000000000099/role",
            json={"role": bad_role},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r2.status_code == 400, f"role {bad_role!r} should be rejected, got {r2.status_code}"
