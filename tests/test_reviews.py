"""Tests for Stage 5: tags, reviews, pre-moderation, aggregate rating."""
from __future__ import annotations

import io
import json
import uuid
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select

from app.core.security import hash_password
from app.models.enums import (
    ReviewStatus,
    UserRole,
)
from app.models.review import Review
from app.models.tag import Tag
from app.models.user import User
from app.services.review_photos import ReviewPhotoService


# ---- Fake blob client for review photos ----
class _FakeStream:
    def __init__(self, data: bytes):
        self._data = data
    async def readall(self):
        return self._data


class FakeReviewBlobClient:
    def __init__(self, store: "FakeReviewBlobStore", container: str, name: str):
        self._store = store
        self._container = container
        self._name = name
        self.url = f"https://fake.blob.core.windows.net/{container}/{name}"

    async def upload_blob(self, data, overwrite=True, content_type=None):
        self._store.blobs[(self._container, self._name)] = data.read()


class FakeReviewBlobStore:
    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}

    def get_container_client(self, container: str):
        outer = self
        class _Container:
            def get_blob_client(self_inner, name: str):
                return FakeReviewBlobClient(outer, container, name)
        return _Container()


@pytest_asyncio.fixture
def fake_review_blob_store(monkeypatch):
    store = FakeReviewBlobStore()

    def patched_init(self, connection_string=None):
        self._conn = connection_string
        self._container = "review-photos"
        self._client = store

    monkeypatch.setattr(ReviewPhotoService, "__init__", patched_init)
    return store


# ---- helpers ----
def _make_png_bytes(width: int = 16, height: int = 16) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(0, 128, 0)).save(buf, format="PNG")
    return buf.getvalue()


async def _make_diner(db, email: str) -> User:
    u = User(
        id=uuid.uuid4(), email=email, password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=UserRole.DINER,
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


async def _make_owner(db, email: str) -> User:
    u = User(
        id=uuid.uuid4(), email=email, password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=UserRole.RESTAURANT_OWNER,
    )
    db.add(u); await db.commit(); await db.refresh(u)
    return u


async def _login(client, email: str) -> str:
    r = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _mint_token(db, email: str) -> str:
    """Bypass /login's rate limit (5/min) for tests that need to mint
    several tokens back-to-back. Goes straight to the auth service so the
    issued JWT is identical to what /login would have returned.
    """
    from app.services.auth_service import login as svc_login
    _, pair = await svc_login(db, email, "supersecret123")
    return pair.access_token


async def _make_restaurant(client, token: str, name: str = "R") -> str:
    r = await client.post(
        "/api/v1/restaurants",
        json={"name": name, "address_line": "1 St", "city": "Houston",
              "state": "TX", "postal_code": "77002", "country": "US",
              "price_range": "2", "cuisine_slugs": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _seed_tags(db, *tuples: tuple[str, str]) -> list[Tag]:
    """Seed tags from (name, slug) tuples; returns the Tag rows."""
    rows: list[Tag] = []
    for name, slug in tuples:
        existing = (await db.execute(select(Tag).where(Tag.slug == slug))).scalar_one_or_none()
        if existing is not None:
            rows.append(existing)
            continue
        t = Tag(name=name, slug=slug, is_active=True)
        db.add(t)
        rows.append(t)
    await db.commit()
    for t in rows:
        await db.refresh(t)
    return rows


# ---- Test: admin tag CRUD ----
@pytest.mark.asyncio
async def test_admin_can_create_list_deactivate_tag(client: AsyncClient, db):
    admin = await _make_admin(db, f"adm-{uuid.uuid4().hex[:6]}@example.com")
    token = await _login(client, admin.email)
    r = await client.post(
        "/api/v1/admin/tags",
        json={"name": "Family-friendly", "category": "vibe"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    tag = r.json()
    assert tag["name"] == "Family-friendly"
    assert tag["slug"] == "family-friendly"
    assert tag["is_active"] is True

    r2 = await client.get("/api/v1/admin/tags", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert any(t["id"] == tag["id"] for t in r2.json())

    r3 = await client.patch(
        f"/api/v1/admin/tags/{tag['id']}",
        json={"is_active": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r3.status_code == 200
    assert r3.json()["is_active"] is False

    r4 = await client.get("/api/v1/tags")  # public
    assert r4.status_code == 200
    assert all(t["id"] != tag["id"] for t in r4.json()), "deactivated tag should not appear publicly"


@pytest.mark.asyncio
async def test_non_admin_cannot_create_tag(client: AsyncClient, db):
    diner = await _make_diner(db, f"diner-{uuid.uuid4().hex[:6]}@example.com")
    token = await _login(client, diner.email)
    r = await client.post(
        "/api/v1/admin/tags",
        json={"name": "Bogus"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


# ---- Test: diner review submission ----
@pytest.mark.asyncio
async def test_diner_can_submit_review_always_pending(client: AsyncClient, db, fake_review_blob_store):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_diner(db, f"diner-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    diner_token = await _login(client, diner.email)
    rid = await _make_restaurant(client, owner_token, "Pending Test")

    png = _make_png_bytes()
    r = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={
            "rating": "5",
            "body": "Absolutely fantastic, will be back next week!",
            "tag_ids": json.dumps([]),
            "instagram_embed_url": "https://www.instagram.com/p/ABC123/",
        },
        files={"file0": ("a.png", io.BytesIO(png), "image/png")},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # Pre-moderation: always PENDING regardless of content
    assert body["moderation_status"] == "pending"
    assert body["flagged"] is False
    assert body["rating"] == 5
    assert len(body["photos"]) == 1
    assert body["photos"][0]["content_type"] == "image/png"


@pytest.mark.asyncio
async def test_more_than_three_tags_rejected_no_truncation(client: AsyncClient, db, fake_review_blob_store):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_diner(db, f"diner-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    diner_token = await _login(client, diner.email)
    rid = await _make_restaurant(client, owner_token, "Tags Test")
    tags = await _seed_tags(db, ("Spicy", "spicy"), ("Sweet", "sweet"),
                              ("Vegan", "vegan"), ("Late-night", "late-night"))

    r = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={
            "rating": "4",
            "body": "Great vibe and quick service, definitely recommend.",
            "tag_ids": json.dumps([t.id for t in tags]),
        },
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    # 4 tags submitted → 400 (no silent truncation, per DoD)
    assert r.status_code == 400, r.text
    assert "too many tags" in r.json()["detail"].lower()

    # And the row wasn't created
    rows = (await db.execute(select(Review))).scalars().all()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_more_than_three_photos_rejected(client: AsyncClient, db, fake_review_blob_store):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_diner(db, f"diner-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    diner_token = await _login(client, diner.email)
    rid = await _make_restaurant(client, owner_token, "Photos Test")

    png = _make_png_bytes()
    r = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "3", "body": "Photos overload but food was fine."},
        files={
            "file0": ("a.png", io.BytesIO(png), "image/png"),
            "file1": ("b.png", io.BytesIO(png), "image/png"),
            "file2": ("c.png", io.BytesIO(png), "image/png"),
            # 4th photo — submit via raw form field to bypass the OpenAPI limit
            "file3": ("d.png", io.BytesIO(png), "image/png"),
        } if False else {
            "file0": ("a.png", io.BytesIO(png), "image/png"),
            "file1": ("b.png", io.BytesIO(png), "image/png"),
            "file2": ("c.png", io.BytesIO(png), "image/png"),
        },
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    # 3 photos OK
    assert r.status_code == 201, r.text
    # Now try 4 with a 4th param trick: we don't have file3 declared on the
    # endpoint so the API will ignore extras, but if the SERVICE allowed
    # 4 it'd slip through. So we test the service-level cap by submitting
    # 3 photos to one review (above) and then directly invoking the service
    # with 4 to assert the cap.
    from app.services import reviews as review_service
    png2 = _make_png_bytes()
    try:
        await review_service.create_review(
            db, restaurant_id=uuid.UUID(rid), reviewer=diner, rating=3,
            body="another one", tag_ids=[],
            photo_uploads=[(png2, "image/png")] * 4,
        )
    except Exception as exc:
        assert "too many photos" in str(exc).lower()
    else:
        raise AssertionError("service should have rejected 4 photos")


@pytest.mark.asyncio
async def test_one_review_per_user_per_restaurant(client: AsyncClient, db, fake_review_blob_store):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_diner(db, f"diner-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    diner_token = await _login(client, diner.email)
    rid = await _make_restaurant(client, owner_token, "Dup Test")

    r1 = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "5", "body": "First review, loved it."},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r1.status_code == 201, r1.text
    r2 = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "3", "body": "Second review, different day."},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r2.status_code == 409, r2.text
    assert "already reviewed" in r2.json()["detail"].lower()


@pytest.mark.asyncio
async def test_owner_cannot_submit_review(client: AsyncClient, db, fake_review_blob_store):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    rid = await _make_restaurant(client, owner_token, "Owner Test")
    r = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "5", "body": "I'm the owner, 5 stars naturally."},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_profanity_triggers_auto_flag(client: AsyncClient, db, fake_review_blob_store):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_diner(db, f"diner-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    diner_token = await _login(client, diner.email)
    rid = await _make_restaurant(client, owner_token, "Profanity Test")
    r = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "1", "body": "This place is total shit, awful service."},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["flagged"] is True
    assert "profanity" in body["flag_reasons"].lower()
    # But still pending
    assert body["moderation_status"] == "pending"


@pytest.mark.asyncio
async def test_duplicate_body_triggers_auto_flag(client: AsyncClient, db, fake_review_blob_store):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_diner(db, f"diner-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    diner_token = await _login(client, diner.email)
    rid1 = await _make_restaurant(client, owner_token, "Dup A")
    rid2 = await _make_restaurant(client, owner_token, "Dup B")
    body_text = "Same exact text, just different whitespace and casing."

    r1 = await client.post(
        f"/api/v1/restaurants/{rid1}/reviews",
        data={"rating": "4", "body": body_text},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r1.status_code == 201, r1.text
    r2 = await client.post(
        f"/api/v1/restaurants/{rid2}/reviews",
        data={"rating": "4", "body": "  " + body_text.upper() + "  "},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r2.status_code == 201, r2.text
    body2 = r2.json()
    assert body2["flagged"] is True
    assert "duplicate" in body2["flag_reasons"].lower()


@pytest.mark.asyncio
async def test_instagram_url_format_validated(client: AsyncClient, db, fake_review_blob_store):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_diner(db, f"diner-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    diner_token = await _login(client, diner.email)
    rid = await _make_restaurant(client, owner_token, "IG Test")

    # Wrong host
    r = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "5", "body": "Good food.", "instagram_embed_url": "https://example.com/p/abc"},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r.status_code == 400
    assert "instagram" in r.json()["detail"].lower()

    # Right host, wrong path
    r = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "5", "body": "Good food.", "instagram_embed_url": "https://www.instagram.com/explore/"},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r.status_code == 400


# ---- Test: admin moderation queue ----
@pytest.mark.asyncio
async def test_admin_queue_lists_pending_with_flagged_first(
    client: AsyncClient, db, fake_review_blob_store
):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"adm-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_diner(db, f"diner-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    admin_token = await _login(client, admin.email)
    diner_token = await _login(client, diner.email)
    rid = await _make_restaurant(client, owner_token, "Queue Test")

    # Clean review (not flagged) — submitted first
    r1 = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "5", "body": "Clean review, no profanity at all, lovely."},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r1.status_code == 201
    # Flagged review — submitted second
    r2 = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "1", "body": "Total shit place, hate it, damn service."},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r2.status_code == 409  # one-review-per-user — re-use the first user

    # Use a different diner for the flagged one
    diner2 = await _make_diner(db, f"diner2-{uuid.uuid4().hex[:6]}@example.com")
    diner2_token = await _login(client, diner2.email)
    r3 = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "1", "body": "Total shit place, hate it, damn service."},
        headers={"Authorization": f"Bearer {diner2_token}"},
    )
    assert r3.status_code == 201, r3.text
    assert r3.json()["flagged"] is True

    # Admin queue
    rq = await client.get(
        "/api/v1/admin/reviews/pending",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert rq.status_code == 200, rq.text
    queue = rq.json()
    assert len(queue) == 2
    # The flagged one should be first
    assert queue[0]["flagged"] is True
    assert queue[1]["flagged"] is False
    # And the `flagged` boolean + `flag_reasons` make the distinction explicit
    assert "profanity" in (queue[0]["flag_reasons"] or "").lower()


# ---- Test: admin approve / reject + aggregate ----
@pytest.mark.asyncio
async def test_admin_approves_review_makes_it_visible_in_aggregate(
    client: AsyncClient, db, fake_review_blob_store
):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"adm-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_diner(db, f"diner-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    admin_token = await _login(client, admin.email)
    diner_token = await _login(client, diner.email)
    rid = await _make_restaurant(client, owner_token, "Approve Test")

    # Submit a 4-star review
    r = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "4", "body": "Great food and service, will return."},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r.status_code == 201
    review_id = r.json()["id"]

    # Pre-approval: aggregate is None, review not in public list
    prof_before = await client.get(f"/api/v1/restaurants/{rid}")
    assert prof_before.status_code == 200
    assert prof_before.json()["aggregate_rating"] is None
    assert prof_before.json()["review_count"] == 0
    assert prof_before.json()["approved_reviews"] == []

    # Admin approves
    appr = await client.post(
        f"/api/v1/admin/reviews/{review_id}/moderate",
        json={"approve": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert appr.status_code == 200
    assert appr.json()["moderation_status"] == "approved"

    # Post-approval: aggregate = 4.0, review in public list
    prof_after = await client.get(f"/api/v1/restaurants/{rid}")
    assert prof_after.json()["aggregate_rating"] == 4.0
    assert prof_after.json()["review_count"] == 1
    assert prof_after.json()["approved_reviews"][0]["id"] == review_id

    # Public list endpoint also sees it
    pub = await client.get(f"/api/v1/restaurants/{rid}/reviews")
    assert pub.status_code == 200
    assert len(pub.json()) == 1


@pytest.mark.asyncio
async def test_admin_rejects_review_keeps_it_out_of_aggregate(
    client: AsyncClient, db, fake_review_blob_store
):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"adm-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_diner(db, f"diner-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    admin_token = await _login(client, admin.email)
    diner_token = await _login(client, diner.email)
    rid = await _make_restaurant(client, owner_token, "Reject Test")

    r = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "1", "body": "This place was a nightmare from start to finish."},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    assert r.status_code == 201
    review_id = r.json()["id"]

    rej = await client.post(
        f"/api/v1/admin/reviews/{review_id}/moderate",
        json={"approve": False, "reason": "Off-topic rant, not about the food."},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert rej.status_code == 200
    assert rej.json()["moderation_status"] == "rejected"
    assert "off-topic" in (rej.json()["rejection_reason"] or "").lower()

    prof = await client.get(f"/api/v1/restaurants/{rid}")
    assert prof.json()["aggregate_rating"] is None
    assert prof.json()["review_count"] == 0


@pytest.mark.asyncio
async def test_pending_reviews_excluded_from_aggregate(
    client: AsyncClient, db, fake_review_blob_store
):
    """DoD: Aggregate rating only counts approved reviews."""
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"adm-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    admin_token = await _login(client, admin.email)
    rid = await _make_restaurant(client, owner_token, "Aggregate Test")

    submitted_ids: list[str] = []
    for i, rating in enumerate([1, 5, 3, 5]):
        diner = await _make_diner(db, f"diner-agg-{i}-{uuid.uuid4().hex[:4]}@example.com")
        token = await _mint_token(db, diner.email)
        r = await client.post(
            f"/api/v1/restaurants/{rid}/reviews",
            data={"rating": str(rating), "body": f"Review number {i}, with text for the platform."},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 201
        submitted_ids.append(r.json()["id"])

    # Approve only the 2nd and 4th (ratings 5 and 5 → avg 5.0)
    for rid_review in [submitted_ids[1], submitted_ids[3]]:
        r = await client.post(
            f"/api/v1/admin/reviews/{rid_review}/moderate",
            json={"approve": True},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
    # Reject the 1st
    r = await client.post(
        f"/api/v1/admin/reviews/{submitted_ids[0]}/moderate",
        json={"approve": False, "reason": "nope"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    # Leave the 3rd (rating 3) PENDING

    prof = await client.get(f"/api/v1/restaurants/{rid}")
    assert prof.json()["aggregate_rating"] == 5.0
    assert prof.json()["review_count"] == 2  # only the two APPROVED 5-stars


@pytest.mark.asyncio
async def test_admin_cannot_moderate_twice(client: AsyncClient, db, fake_review_blob_store):
    owner = await _make_owner(db, f"own-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"adm-{uuid.uuid4().hex[:6]}@example.com")
    diner = await _make_diner(db, f"diner-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    admin_token = await _login(client, admin.email)
    diner_token = await _login(client, diner.email)
    rid = await _make_restaurant(client, owner_token, "Re-mod Test")
    r = await client.post(
        f"/api/v1/restaurants/{rid}/reviews",
        data={"rating": "3", "body": "Decent place, nothing special today."},
        headers={"Authorization": f"Bearer {diner_token}"},
    )
    review_id = r.json()["id"]
    a1 = await client.post(
        f"/api/v1/admin/reviews/{review_id}/moderate",
        json={"approve": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert a1.status_code == 200
    a2 = await client.post(
        f"/api/v1/admin/reviews/{review_id}/moderate",
        json={"approve": False, "reason": "changed my mind"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert a2.status_code == 409
