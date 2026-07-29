"""Rate limit: rapid repeated login / password-reset requests get blocked with 429."""
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_login_rate_limited_after_5_attempts_per_minute(client: AsyncClient):
    """Login is 5/minute — 6th and 7th hits should be 429."""
    payload = {"email": "ratelimit@example.com", "password": "wrong"}
    statuses = []
    for _ in range(7):
        r = await client.post("/api/v1/auth/login", json=payload)
        statuses.append(r.status_code)
    # First 5 are 401 (bad creds), then 429 kicks in.
    assert statuses[:5] == [401, 401, 401, 401, 401], statuses
    assert statuses[5] == 429, statuses
    assert statuses[6] == 429, statuses


@pytest.mark.asyncio
async def test_password_reset_request_rate_limited_after_3_per_hour(client: AsyncClient):
    """Password-reset request is 3/hour — 4th hit should be 429."""
    payload = {"email": "ratelimit2@example.com"}
    statuses = []
    for _ in range(5):
        r = await client.post("/api/v1/auth/password-reset/request", json=payload)
        statuses.append(r.status_code)
    assert statuses[:3] == [204, 204, 204], statuses
    assert statuses[3] == 429, statuses
    assert statuses[4] == 429, statuses
