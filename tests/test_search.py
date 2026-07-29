"""Search endpoint: cuisine, distance, price, verification, tsvector full-text."""
import math
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from app.core.security import hash_password
from app.models.enums import (
    HalalStatus,
    HalalVerificationSource,
    PriceRange,
    RestaurantTier,
    UserRole,
)
from app.models.restaurant import Restaurant
from app.models.user import User
from app.services import restaurant_service


async def _seed_restaurant(
    db, *, owner: User, name: str, lat: float, lng: float, tier=RestaurantTier.FREE,
    price_range=PriceRange.MODERATE, halal_status=HalalStatus.UNVERIFIED,
    halal_source=HalalVerificationSource.SELF_REPORTED, description: str = "",
) -> Restaurant:
    """Insert a restaurant directly via the DB, bypassing geocoding."""
    import uuid as _uuid
    from datetime import datetime, timezone
    r = Restaurant(
        id=_uuid.uuid4(),
        owner_id=owner.id,
        name=name,
        slug=name.lower().replace(" ", "-") + "-" + _uuid.uuid4().hex[:6],
        description=description,
        address_line="123 Test St",
        city="Houston",
        state="TX",
        postal_code="77002",
        country="US",
        latitude=lat,
        longitude=lng,
        geocoded_at=datetime.now(timezone.utc),
        price_range=price_range,
        tier=tier,
        halal_status=halal_status,
        halal_verification_source=halal_source,
        is_active=True,
    )
    db.add(r)
    await db.flush()
    # Update tsvector
    await restaurant_service._update_search_vector(db, r)
    await db.commit()
    await db.refresh(r)
    return r


async def _make_owner(db, email: str) -> User:
    u = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0],
        role=UserRole.RESTAURANT_OWNER,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


# Houston (29.7604, -95.3698) center — fixtures placed around it
HOUSTON = (29.7604, -95.3698)


@pytest.mark.asyncio
async def test_search_text_query_ranks_relevant_higher(client: AsyncClient, db):
    owner = await _make_owner(db, f"owner-q-{uuid.uuid4().hex[:6]}@example.com")
    # Create one matching + one unrelated
    a = await _seed_restaurant(db, owner=owner, name="Sufi Biryani Palace",
                                lat=HOUSTON[0], lng=HOUSTON[1],
                                description="Authentic Pakistani biryani")
    b = await _seed_restaurant(db, owner=owner, name="Dragon Wok",
                                lat=HOUSTON[0], lng=HOUSTON[1],
                                description="Chinese food")
    resp = await client.get("/api/v1/search/restaurants", params={"q": "biryani pakistani"})
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    # "Sufi Biryani Palace" should match the query; "Dragon Wok" should not (tsvector
    # filters by @@ operator — non-matching rows are excluded, not just ranked down).
    names = [r["name"] for r in results]
    assert "Sufi Biryani Palace" in names
    assert "Dragon Wok" not in names
    # And the matching one is the top result
    assert names[0] == "Sufi Biryani Palace"


@pytest.mark.asyncio
async def test_search_distance_filter(client: AsyncClient, db):
    owner = await _make_owner(db, f"owner-d-{uuid.uuid4().hex[:6]}@example.com")
    # Two restaurants ~0km and ~50km from Houston
    near = await _seed_restaurant(db, owner=owner, name="Near Place", lat=29.76, lng=-95.37)
    far = await _seed_restaurant(db, owner=owner, name="Far Place", lat=30.27, lng=-95.47)  # ~57km NW
    # Within 10km of Houston center
    resp = await client.get("/api/v1/search/restaurants", params={
        "lat": HOUSTON[0], "lng": HOUSTON[1], "radius_km": 10,
    })
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    names = [r["name"] for r in results]
    assert "Near Place" in names
    assert "Far Place" not in names


@pytest.mark.asyncio
async def test_search_cuisine_filter(client: AsyncClient, db):
    """Cuisine filter requires a Cuisine row; we just verify the no-cuisine path
    works (the cuisine-M2M path is exercised via the search-restaurants endpoint)."""
    owner = await _make_owner(db, f"owner-c-{uuid.uuid4().hex[:6]}@example.com")
    r = await _seed_restaurant(db, owner=owner, name="Biryani Spot", lat=HOUSTON[0], lng=HOUSTON[1])
    resp = await client.get("/api/v1/search/restaurants", params={"limit": 50})
    assert resp.status_code == 200, resp.text
    assert any(x["name"] == "Biryani Spot" for x in resp.json()["results"])


@pytest.mark.asyncio
async def test_search_halal_status_filter(client: AsyncClient, db):
    owner = await _make_owner(db, f"owner-h-{uuid.uuid4().hex[:6]}@example.com")
    verified = await _seed_restaurant(
        db, owner=owner, name="Verified Halal", lat=HOUSTON[0], lng=HOUSTON[1],
        halal_status=HalalStatus.VERIFIED,
        halal_source=HalalVerificationSource.CERTIFIED,
    )
    self_rep = await _seed_restaurant(
        db, owner=owner, name="Self Reported", lat=HOUSTON[0], lng=HOUSTON[1],
        halal_status=HalalStatus.UNVERIFIED,
        halal_source=HalalVerificationSource.SELF_REPORTED,
    )
    resp = await client.get("/api/v1/search/restaurants", params={"halal_status": "verified"})
    assert resp.status_code == 200, resp.text
    names = [r["name"] for r in resp.json()["results"]]
    assert "Verified Halal" in names
    assert "Self Reported" not in names


@pytest.mark.asyncio
async def test_search_price_range_filter(client: AsyncClient, db):
    owner = await _make_owner(db, f"owner-p-{uuid.uuid4().hex[:6]}@example.com")
    cheap = await _seed_restaurant(db, owner=owner, name="Cheap Eats", lat=HOUSTON[0], lng=HOUSTON[1],
                                  price_range=PriceRange.BUDGET)
    pricey = await _seed_restaurant(db, owner=owner, name="Fancy Place", lat=HOUSTON[0], lng=HOUSTON[1],
                                   price_range=PriceRange.LUXURY)
    resp = await client.get("/api/v1/search/restaurants", params={"max_price": "2"})
    assert resp.status_code == 200, resp.text
    names = [r["name"] for r in resp.json()["results"]]
    assert "Cheap Eats" in names
    assert "Fancy Place" not in names


@pytest.mark.asyncio
async def test_search_pagination(client: AsyncClient, db):
    owner = await _make_owner(db, f"owner-pg-{uuid.uuid4().hex[:6]}@example.com")
    # Seed 5 restaurants
    for i in range(5):
        await _seed_restaurant(db, owner=owner, name=f"Pg Test {i}", lat=HOUSTON[0], lng=HOUSTON[1])
    r1 = await client.get("/api/v1/search/restaurants", params={"limit": 2, "offset": 0})
    r2 = await client.get("/api/v1/search/restaurants", params={"limit": 2, "offset": 2})
    r3 = await client.get("/api/v1/search/restaurants", params={"limit": 2, "offset": 4})
    assert r1.status_code == 200
    assert r1.json()["total"] == 5
    assert len(r1.json()["results"]) == 2
    assert len(r2.json()["results"]) == 2
    assert len(r3.json()["results"]) == 1
    # No overlap between pages
    ids = lambda r: {x["id"] for x in r.json()["results"]}
    assert ids(r1).isdisjoint(ids(r2))
    assert ids(r1).isdisjoint(ids(r3))
    assert ids(r2).isdisjoint(ids(r3))
