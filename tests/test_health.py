"""Smoke test for the /health endpoint.

Accepts both 200 (DB reachable) and 503 (DB down) so the suite is green on
boxes without Postgres while still proving the endpoint shape is correct.
Stage 2+ will tighten this with a transactional DB fixture.
"""
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_well_formed_payload(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code in (200, 503)

    body = resp.json()
    assert body["status"] in ("ok", "degraded")
    assert body["db"] in ("ok", "error")
    assert "env" in body


@pytest.mark.asyncio
async def test_health_200_when_db_reachable(client: AsyncClient) -> None:
    """If we get 200, the DB ping actually worked — no point faking it."""
    resp = await client.get("/health")
    if resp.status_code == 200:
        assert resp.json()["db"] == "ok"
