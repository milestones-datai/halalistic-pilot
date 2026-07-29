"""Stage 9 — sharing & OG meta & deal-card image generation.

Covers:
  - share URL shape (deal + restaurant)
  - HTML meta tags (og:* / twitter:* / canonical)
  - HTML escaping of XSS-y input
  - PNG generation (story 1080x1920 + OG 1200x630)
  - Deal card endpoints (public, no auth)
  - 404 paths
"""
from __future__ import annotations

import io
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from PIL import Image
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security import hash_password
from app.models.deal import Deal, DealStatus, DealType, DealAudience
from app.models.enums import RestaurantTier, UserRole
from app.models.restaurant import Restaurant
from app.models.user import User
from app.services.sharing import (
    deal_share_url,
    restaurant_share_url,
    build_deal_og_html,
    build_restaurant_og_html,
)


# ---- URL helpers ----
def test_deal_share_url_shape():
    url = deal_share_url("abc-123")
    assert url.endswith("/share/deals/abc-123")
    assert url.startswith("http")


def test_restaurant_share_url_shape():
    url = restaurant_share_url("rid")
    assert url.endswith("/share/restaurants/rid")


# ---- OG HTML ----
def test_og_html_contains_all_meta():
    """Pure-Python — no DB needed. We pass simple objects whose
    attributes mirror the ORM models (id, title, description, etc.).
    """
    class _Deal:
        id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        title = "20% off"
        description = "Best deal ever"
    class _Rest:
        id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        name = "Riyadh Palace"
        description = "Authentic"
        address_line = "123 Main"
        city = "Houston"
    d = _Deal(); r = _Rest()
    html = build_deal_og_html(d, r)
    for needle in (
        '<meta property="og:title"',
        '<meta property="og:description"',
        '<meta property="og:url"',
        '<meta property="og:image"',
        '<meta name="twitter:card" content="summary_large_image"',
        '<link rel="canonical"',
        '<title>',
    ):
        assert needle in html, f"missing {needle!r}"


def test_og_html_escapes_user_input():
    class _Deal:
        id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        title = "Free hummus <script>alert(1)</script>"
        description = "Best deal ever & more"
    class _Rest:
        id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        name = "Riyadh & Co"
        description = "Authentic"
        address_line = "123 Main"
        city = "Houston"
    html = build_deal_og_html(_Deal(), _Rest())
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert "Best deal ever &amp; more" in html
    assert "Riyadh &amp; Co" in html


def test_restaurant_og_html_includes_share_image():
    class _Rest:
        id = uuid.UUID("22222222-2222-2222-2222-222222222222")
        name = "Y"
        description = "Authentic"
        address_line = "123 Main"
        city = "Houston"
    html = build_restaurant_og_html(_Rest())
    assert "/share/restaurants/" in html
    assert 'property="og:image"' in html


# ---- PNG generation ----
class _FakeHalal:
    def __init__(self, v): self.value = v
class _FakeRestaurant:
    def __init__(self, name, halal_status, address_line="a", city="c"):
        self.name = name
        self.address_line = address_line
        self.city = city
        self.halal_status = _FakeHalal(halal_status)
class _FakeDeal:
    def __init__(self, title, description="", deal_type=None,
                 start_date=None, end_date=None):
        self.title = title
        self.description = description
        self.deal_type = deal_type or _FakeHalal("percent_off")
        self.start_date = start_date
        self.end_date = end_date


def test_deal_card_png_dimensions():
    r = _FakeRestaurant("X", "verified")
    d = _FakeDeal("Test deal", description="description")
    from app.services.deal_cards import render_deal_card
    png_story = render_deal_card(d, r, size="story")
    png_og = render_deal_card(d, r, size="og")
    assert png_story[:8] == b"\x89PNG\r\n\x1a\n"
    assert png_og[:8] == b"\x89PNG\r\n\x1a\n"
    img_s = Image.open(io.BytesIO(png_story))
    img_o = Image.open(io.BytesIO(png_og))
    assert img_s.size == (1080, 1920)
    assert img_o.size == (1200, 630)


def test_restaurant_only_card_png():
    r = _FakeRestaurant("Y", "unverified")
    from app.services.deal_cards import render_deal_card
    png = render_deal_card(deal=None, restaurant=r, size="story")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png))
    assert img.size == (1080, 1920)


# ---- DB-backed fixtures for HTTP tests ----
async def _make_owner(db, email: str) -> User:
    u = User(
        id=uuid.uuid4(), email=email, password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=UserRole.RESTAURANT_OWNER,
    )
    db.add(u); await db.commit(); await db.refresh(u)
    return u


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


async def _make_deal(db, restaurant: Restaurant, owner: User) -> Deal:
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
        status=DealStatus.APPROVED,
    )
    db.add(d); await db.commit(); await db.refresh(d)
    return d


# ---- HTTP endpoints ----
@pytest.mark.asyncio
async def test_share_deal_page_returns_200(client: AsyncClient, db):
    owner = await _make_owner(db, "owner1@x.com")
    r = await _make_restaurant(db, owner)
    d = await _make_deal(db, r, owner)
    resp = await client.get(f"/api/v1/share/deals/{d.id}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "og:title" in resp.text
    assert "og:image" in resp.text


@pytest.mark.asyncio
async def test_share_deal_card_png_returns_image(client: AsyncClient, db):
    owner = await _make_owner(db, "owner2@x.com")
    r = await _make_restaurant(db, owner)
    d = await _make_deal(db, r, owner)
    resp = await client.get(f"/api/v1/share/deals/{d.id}/card.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (1080, 1920)


@pytest.mark.asyncio
async def test_share_deal_card_og_png_returns_1200x630(client: AsyncClient, db):
    owner = await _make_owner(db, "owner3@x.com")
    r = await _make_restaurant(db, owner)
    d = await _make_deal(db, r, owner)
    resp = await client.get(f"/api/v1/share/deals/{d.id}/card-og.png")
    assert resp.status_code == 200
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (1200, 630)


@pytest.mark.asyncio
async def test_share_deal_404(client: AsyncClient):
    resp = await client.get(f"/api/v1/share/deals/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_share_restaurant_page(client: AsyncClient, db):
    owner = await _make_owner(db, "owner4@x.com")
    r = await _make_restaurant(db, owner)
    resp = await client.get(f"/api/v1/share/restaurants/{r.id}")
    assert resp.status_code == 200
    assert "og:title" in resp.text
    assert "Halalistic" in resp.text


@pytest.mark.asyncio
async def test_share_restaurant_card_png(client: AsyncClient, db):
    owner = await _make_owner(db, "owner5@x.com")
    r = await _make_restaurant(db, owner)
    resp = await client.get(f"/api/v1/share/restaurants/{r.id}/card.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (1080, 1920)
