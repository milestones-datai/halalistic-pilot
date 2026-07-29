"""Stage 9 — web push + email + SMS guard.

Covers:
  - VAPID public-key endpoint (no auth) returns a key
  - POST /push/subscribe creates a row (idempotent on endpoint)
  - POST /push/subscribe requires auth
  - DELETE /push/subscribe removes by endpoint
  - Approving a deal triggers push (mocked webpush) + email
  - Stale endpoint (404) is removed automatically
  - AzureACSBackend rejects PLACEHOLDER (factory falls back to console_log)
  - SmsBackend raises NotImplementedError (no Twilio in the project)
  - pyproject.toml has no Twilio dependency
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security import hash_password
from app.models.deal import Deal, DealStatus, DealType, DealAudience
from app.models.enums import RestaurantTier, UserRole
from app.models.push import PushSubscription
from app.models.restaurant import Restaurant
from app.models.user import User


# ---- helpers ----
async def _make_user(db, email: str, role: UserRole) -> User:
    u = User(
        id=uuid.uuid4(), email=email, password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=role,
    )
    db.add(u); await db.commit(); await db.refresh(u)
    return u


async def _make_diner(db, email: str) -> User:
    return await _make_user(db, email, UserRole.DINER)


async def _make_owner(db, email: str) -> User:
    return await _make_user(db, email, UserRole.RESTAURANT_OWNER)


async def _make_curator(db, email: str) -> User:
    return await _make_user(db, email, UserRole.DEAL_CURATOR)


async def _mint_token(client, email: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={
        "email": email, "password": "supersecret123",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _make_restaurant(db, owner: User) -> Restaurant:
    r = Restaurant(
        id=uuid.uuid4(),
        owner_id=owner.id,
        name="Riyadh Palace",
        slug="riyadh-palace",
        description="A great halal spot.",
        address_line="123 Main St",
        city="Houston",
        state="TX",
        postal_code="77001",
        latitude=29.7604,
        longitude=-95.3698,
        halal_status="verified",
        tier=RestaurantTier.FREE,
    )
    db.add(r); await db.commit(); await db.refresh(r)
    return r


async def _make_pending_deal(db, restaurant: Restaurant, owner: User) -> Deal:
    d = Deal(
        id=uuid.uuid4(),
        restaurant_id=restaurant.id,
        created_by=owner.id,
        title="20% off",
        description="Best deal ever",
        deal_type=DealType.PERCENTAGE_OFF,
        target_audience=DealAudience.PUBLIC,
        discount_value=Decimal("20"),
        start_date=date.today(),
        end_date=date.today() + timedelta(days=7),
        status=DealStatus.PENDING_REVIEW,
    )
    db.add(d); await db.commit(); await db.refresh(d)
    return d


# ---- VAPID public key ----
@pytest.mark.asyncio
async def test_vapid_public_key_endpoint(client: AsyncClient):
    resp = await client.get("/api/v1/push/public-key")
    assert resp.status_code == 200
    body = resp.json()
    # Either a real key, or None if the bootstrap hasn't run yet (it's
    # lazy). The contract is "field exists, type Optional[str]".
    assert "public_key" in body
    assert body["public_key"] is None or isinstance(body["public_key"], str)


# ---- Subscribe requires auth ----
@pytest.mark.asyncio
async def test_subscribe_requires_auth(client: AsyncClient, db):
    owner = await _make_owner(db, "authowner1@x.com")
    r = await _make_restaurant(db, owner)
    resp = await client.post("/api/v1/push/subscribe", json={
        "restaurant_id": str(r.id),
        "endpoint": "https://fcm.googleapis.com/x",
        "keys": {"p256dh": "abc", "auth": "def"},
    })
    assert resp.status_code == 401


# ---- Subscribe idempotency ----
@pytest.mark.asyncio
async def test_subscribe_creates_and_is_idempotent(client: AsyncClient, db):
    owner = await _make_owner(db, "authowner2@x.com")
    r = await _make_restaurant(db, owner)
    diner = await _make_diner(db, "diner1@x.com")
    token = await _mint_token(client, diner.email)
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "restaurant_id": str(r.id),
        "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
        "keys": {"p256dh": "BIPUL12", "auth": "AUTH12"},
        "user_agent": "Mozilla/5.0",
    }
    resp1 = await client.post("/api/v1/push/subscribe", json=body, headers=headers)
    assert resp1.status_code == 200, resp1.text
    resp2 = await client.post("/api/v1/push/subscribe", json=body, headers=headers)
    assert resp2.status_code == 200, resp2.text
    rows = list((await db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == body["endpoint"])
    )).scalars().all())
    assert len(rows) == 1  # idempotent on endpoint


@pytest.mark.asyncio
async def test_subscribe_rejects_missing_keys(client: AsyncClient, db):
    owner = await _make_owner(db, "authowner3@x.com")
    r = await _make_restaurant(db, owner)
    diner = await _make_diner(db, "diner2@x.com")
    token = await _mint_token(client, diner.email)
    resp = await client.post("/api/v1/push/subscribe", json={
        "restaurant_id": str(r.id),
        "endpoint": "https://example.com/sub",
        "keys": {"p256dh": "x"},  # missing auth
    }, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400


# ---- Unsubscribe ----
@pytest.mark.asyncio
async def test_unsubscribe_removes_row(client: AsyncClient, db):
    owner = await _make_owner(db, "authowner4@x.com")
    r = await _make_restaurant(db, owner)
    diner = await _make_diner(db, "diner3@x.com")
    token = await _mint_token(client, diner.email)
    headers = {"Authorization": f"Bearer {token}"}
    ep = "https://example.com/sub-1"
    await client.post("/api/v1/push/subscribe", json={
        "restaurant_id": str(r.id),
        "endpoint": ep,
        "keys": {"p256dh": "x", "auth": "y"},
    }, headers=headers)
    resp = await client.request(
        "DELETE", "/api/v1/push/subscribe",
        json={"endpoint": ep}, headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    rows = list((await db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == ep)
    )).scalars().all())
    assert rows == []


# ---- Approve triggers push (mocked) + email ----
@pytest.mark.asyncio
async def test_approve_triggers_push_and_email(client: AsyncClient, db):
    """When a curator approves a deal, the system should:
       (a) call webpush once per subscribed user
       (b) send a "new deal" marketing email to every diner
    """
    owner = await _make_owner(db, "authowner5@x.com")
    r = await _make_restaurant(db, owner)
    d = await _make_pending_deal(db, r, owner)
    diner = await _make_diner(db, "diner4@x.com")
    diner2 = await _make_diner(db, "diner5@x.com")
    # Subscribe both diners to this restaurant's push.
    for u in (diner, diner2):
        token = await _mint_token(client, u.email)
        await client.post("/api/v1/push/subscribe", json={
            "restaurant_id": str(r.id),
            "endpoint": f"https://fcm.googleapis.com/fcm/send/{u.id}",
            "keys": {"p256dh": "x", "auth": "y"},
        }, headers={"Authorization": f"Bearer {token}"})

    # Mock the actual webpush call to avoid hitting Google.
    from pywebpush import WebPushException
    class _FakeResponse:
        status_code = 200
    calls: list[dict[str, Any]] = []
    def fake_webpush(subscription_info, data, vapid_private_key, vapid_claims, **kwargs):
        calls.append({"endpoint": subscription_info["endpoint"],
                      "data": data, "vapid_claims": vapid_claims})

    # Spy on the email send.
    from app.services import email as email_service
    sent: list[tuple[str, str]] = []
    real_send = email_service.send
    def spy_send(msg):
        sent.append((msg.to, msg.subject))
    email_service._backend = None  # force re-resolve
    monkey = pytest.MonkeyPatch()
    monkey.setattr(email_service, "send", spy_send)
    monkey.setattr("app.services.push.webpush", fake_webpush)
    try:
        curator = await _make_curator(db, "curator1@x.com")
        ctoken = await _mint_token(client, curator.email)
        resp = await client.post(
            f"/api/v1/admin/deals/{d.id}/approve",
            headers={"Authorization": f"Bearer {ctoken}"},
        )
        assert resp.status_code == 200, resp.text
    finally:
        monkey.undo()

    # webpush called once per subscription
    assert len(calls) == 2
    eps = {c["endpoint"] for c in calls}
    assert f"https://fcm.googleapis.com/fcm/send/{diner.id}" in eps
    # Subject of the new-deal email reaches every diner
    recipients = {to for to, _ in sent}
    assert diner.email in recipients
    assert diner2.email in recipients
    assert all("New deal" in subj or "new deal" in subj for _, subj in sent)


@pytest.mark.asyncio
async def test_stale_push_endpoint_is_removed(client: AsyncClient, db):
    """A 404 from a push service should remove the subscription row."""
    owner = await _make_owner(db, "authowner6@x.com")
    r = await _make_restaurant(db, owner)
    d = await _make_pending_deal(db, r, owner)
    diner = await _make_diner(db, "diner6@x.com")
    token = await _mint_token(client, diner.email)
    ep = "https://fcm.googleapis.com/fcm/send/stale-1"
    await client.post("/api/v1/push/subscribe", json={
        "restaurant_id": str(r.id),
        "endpoint": ep,
        "keys": {"p256dh": "x", "auth": "y"},
    }, headers={"Authorization": f"Bearer {token}"})
    # Sanity: 1 row.
    rows = list((await db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == ep)
    )).scalars().all())
    assert len(rows) == 1

    from pywebpush import WebPushException
    class _FakeResp:
        status_code = 404
    def fake_webpush_404(**kwargs):
        raise WebPushException("gone", response=_FakeResp())

    monkey = pytest.MonkeyPatch()
    monkey.setattr("app.services.push.webpush", fake_webpush_404)
    try:
        curator = await _make_curator(db, "curator2@x.com")
        ctoken = await _mint_token(client, curator.email)
        resp = await client.post(
            f"/api/v1/admin/deals/{d.id}/approve",
            headers={"Authorization": f"Bearer {ctoken}"},
        )
        assert resp.status_code == 200
    finally:
        monkey.undo()

    # Row should be removed after 404.
    rows = list((await db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == ep)
    )).scalars().all())
    assert rows == []


# ---- Email backend guards ----
def test_acs_rejects_placeholder_connection_string():
    from app.services.email.azure_acs import AzureACSBackend
    with pytest.raises(RuntimeError, match="(?i)placeholder"):
        AzureACSBackend(
            connection_string="endpoint=https://PLACEHOLDER;accesskey=PLACEHOLDER",
            sender_address="noreply@halalistic.com",
        )


def test_acs_rejects_placeholder_sender_address():
    from app.services.email.azure_acs import AzureACSBackend
    with pytest.raises(RuntimeError, match="(?i)placeholder"):
        AzureACSBackend(
            connection_string="endpoint=https://x;accesskey=x",
            sender_address="PLACEHOLDER@x.com",
        )


def test_acs_accepted_with_real_creds():
    """We don't actually connect — just confirm the guard doesn't
    reject. Real connection only happens on .send()."""
    from app.services.email.azure_acs import AzureACSBackend
    b = AzureACSBackend(
        connection_string="endpoint=https://real.communication.azure.com/;accesskey=ABC=",
        sender_address="Halalistic <noreply@halalistic.com>",
    )
    assert b._sender == "Halalistic <noreply@halalistic.com>"


def test_factory_falls_back_to_console_log_when_acs_init_fails(monkeypatch):
    """If ACS is selected but config has PLACEHOLDER, the factory
    should fall back to ConsoleLog (never silently lose email)."""
    from app.services import email as email_service
    from app.services.email.console_log import ConsoleLogBackend
    monkeypatch.setattr(email_service.settings, "email_backend", "azure_acs")
    monkeypatch.setattr(email_service.settings, "azure_communication_connection_string",
                        "endpoint=PLACEHOLDER;accesskey=PLACEHOLDER")
    monkeypatch.setattr(email_service.settings, "azure_communication_sender_address",
                        "PLACEHOLDER@x.com")
    email_service._backend = None
    backend = email_service.get_email_backend()
    assert isinstance(backend, ConsoleLogBackend)


def test_sms_backend_protocol_raises_not_implemented():
    """Stage 9 ships NO SMS provider. SmsBackend is an explicit
    extension point that raises NotImplementedError so the Phase 2
    task is obvious."""
    from app.services.email.sms import SmsBackend, SmsMessage, SmsNotImplemented
    with pytest.raises(NotImplementedError):
        SmsNotImplemented().send(SmsMessage(to="+15551234567", body="hi"))


# ---- pyproject.toml — no Twilio, no SMS provider ----
def test_no_twilio_dependency():
    """Defense in depth: grep pyproject.toml to confirm no Twilio SDK
    shipped (SMS is Phase 2, per BRD §3.8 / §9.2 F-031)."""
    p = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = p.read_text(encoding="utf-8").lower()
    assert "twilio" not in text
    assert "vonage" not in text
    assert "messagebird" not in text
