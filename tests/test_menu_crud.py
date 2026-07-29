"""Menu CRUD: 4-level Category → Subcategory → Item → Variant."""
import uuid

import pytest
from httpx import AsyncClient

from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.user import User


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


async def _login(client, email: str) -> str:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_menu_appears_in_profile(client: AsyncClient, db):
    owner = await _make_owner(db, f"menu-{uuid.uuid4().hex[:6]}@example.com")
    token = await _login(client, owner.email)
    r = await client.post(
        "/api/v1/restaurants",
        json={"name": "Menu Test", "address_line": "1 St", "city": "Houston",
              "state": "TX", "postal_code": "77002", "country": "US",
              "price_range": "2", "cuisine_slugs": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    rid = r.json()["id"]
    # Category → subcategory → item → variant
    cat_resp = await client.post(
        f"/api/v1/restaurants/{rid}/menu/categories",
        json={"name": "Mains", "sort_order": 0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert cat_resp.status_code == 201
    cat_id = cat_resp.json()["id"]
    sub_resp = await client.post(
        f"/api/v1/restaurants/{rid}/menu/categories/{cat_id}/subcategories",
        json={"name": "Lunch", "sort_order": 0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert sub_resp.status_code == 201
    sub_id = sub_resp.json()["id"]
    item_resp = await client.post(
        f"/api/v1/restaurants/{rid}/menu/items",
        json={"category_id": cat_id, "subcategory_id": sub_id, "name": "Chicken Biryani",
              "description": "Saffron rice, zabiha halal chicken", "base_price_cents": 1499},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert item_resp.status_code == 201
    item_id = item_resp.json()["id"]
    var_resp = await client.post(
        f"/api/v1/restaurants/{rid}/menu/items/{item_id}/variants",
        json={"name": "Large", "price_cents": 1899, "is_default": False, "sort_order": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert var_resp.status_code == 201

    # Verify the profile aggregates correctly
    prof = await client.get(f"/api/v1/restaurants/{rid}")
    menu = prof.json()["menu"]
    assert len(menu) == 1
    mains = menu[0]
    assert mains["name"] == "Mains"
    assert len(mains["subcategories"]) == 1
    lunch = mains["subcategories"][0]
    assert lunch["name"] == "Lunch"
    assert len(lunch["items"]) == 1
    biryani = lunch["items"][0]
    assert biryani["name"] == "Chicken Biryani"
    assert biryani["base_price_cents"] == 1499
    assert biryani["description"] == "Saffron rice, zabiha halal chicken"
    assert len(biryani["variants"]) == 1
    assert biryani["variants"][0]["name"] == "Large"
    assert biryani["variants"][0]["price_cents"] == 1899


@pytest.mark.asyncio
async def test_menu_direct_item_under_category_no_subcategory(client: AsyncClient, db):
    """Items can live directly under a category without a subcategory."""
    owner = await _make_owner(db, f"direct-{uuid.uuid4().hex[:6]}@example.com")
    token = await _login(client, owner.email)
    r = await client.post(
        "/api/v1/restaurants",
        json={"name": "Direct Test", "address_line": "1 St", "city": "Houston",
              "state": "TX", "postal_code": "77002", "country": "US",
              "price_range": "2", "cuisine_slugs": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    rid = r.json()["id"]
    cat_resp = await client.post(
        f"/api/v1/restaurants/{rid}/menu/categories",
        json={"name": "Appetizers"},
        headers={"Authorization": f"Bearer {token}"},
    )
    cat_id = cat_resp.json()["id"]
    item_resp = await client.post(
        f"/api/v1/restaurants/{rid}/menu/items",
        json={"category_id": cat_id, "name": "Samosa", "base_price_cents": 399},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert item_resp.status_code == 201

    prof = await client.get(f"/api/v1/restaurants/{rid}")
    menu = prof.json()["menu"]
    assert len(menu) == 1
    assert menu[0]["subcategories"] == []
    assert len(menu[0]["items"]) == 1
    assert menu[0]["items"][0]["name"] == "Samosa"


@pytest.mark.asyncio
async def test_other_owner_cannot_add_menu(client: AsyncClient, db):
    """Ownership RBAC extends to menu endpoints."""
    a = await _make_owner(db, f"a-{uuid.uuid4().hex[:6]}@example.com")
    b = await _make_owner(db, f"b-{uuid.uuid4().hex[:6]}@example.com")
    a_token = await _login(client, a.email)
    b_token = await _login(client, b.email)
    r = await client.post(
        "/api/v1/restaurants",
        json={"name": "Menu RBAC", "address_line": "1 St", "city": "Houston",
              "state": "TX", "postal_code": "77002", "country": "US",
              "price_range": "2", "cuisine_slugs": []},
        headers={"Authorization": f"Bearer {a_token}"},
    )
    rid = r.json()["id"]
    # B tries to add a category to A's restaurant
    r2 = await client.post(
        f"/api/v1/restaurants/{rid}/menu/categories",
        json={"name": "Hijack"},
        headers={"Authorization": f"Bearer {b_token}"},
    )
    assert r2.status_code == 403
