"""Tests for JWT validation: expiry, signature, type confusion."""
import time
import uuid

import jwt
import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.core.security import create_access_token


async def _register_diner(client: AsyncClient, email: str) -> dict:
    r = await client.post("/api/v1/auth/register", json={
        "email": email, "password": "supersecret123", "display_name": "U", "role": "diner",
    })
    return r.json()


@pytest.mark.asyncio
async def test_expired_access_token_is_rejected(client: AsyncClient):
    await _register_diner(client, "t1@example.com")
    bad = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": "diner",
            "type": "access",
            "iat": int(time.time()) - 3600,
            "exp": int(time.time()) - 60,
        },
        settings.secret_key,
        algorithm="HS256",
    )
    resp = await client.post(
        "/api/v1/admin/users/00000000-0000-0000-0000-000000000000/role",
        json={"role": "deal_curator"},
        headers={"Authorization": f"Bearer {bad}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wrong_signature_is_rejected(client: AsyncClient):
    bad = jwt.encode(
        {"sub": "x", "type": "access", "exp": int(time.time()) + 600},
        "wrong-secret-key",
        algorithm="HS256",
    )
    resp = await client.post(
        "/api/v1/admin/users/00000000-0000-0000-0000-000000000000/role",
        json={"role": "deal_curator"},
        headers={"Authorization": f"Bearer {bad}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_token_cannot_be_used_as_access(client: AsyncClient):
    body = await _register_diner(client, "t2@example.com")
    refresh = body["refresh_token"]
    resp = await client.post(
        "/api/v1/admin/users/00000000-0000-0000-0000-000000000000/role",
        json={"role": "deal_curator"},
        headers={"Authorization": f"Bearer {refresh}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_access_token_works_on_admin_with_admin_role(client, db):
    """Smoke: a real access token with a valid user passes the get_current_user dep."""
    from app.core.security import hash_password
    from app.models.enums import UserRole
    from app.models.user import User
    admin = User(
        id=uuid.uuid4(),
        email="root2@example.com",
        password_hash=hash_password("supersecret123"),
        display_name="Root",
        role=UserRole.PLATFORM_ADMIN,
    )
    db.add(admin)
    await db.commit()
    token = create_access_token(admin.id, admin.role.value)
    resp = await client.post(
        f"/api/v1/admin/users/{uuid.uuid4()}/role",
        json={"role": "deal_curator"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # 404 is fine (no such user) — the point is that we got PAST the 401/403 gate.
    assert resp.status_code in (200, 404), resp.text
