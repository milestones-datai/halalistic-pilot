"""Restaurant + photo + menu business logic: CRUD, ownership, search, profile."""
from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import and_, bindparam, func, or_, select, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    HalalStatus,
    HalalVerificationSource,
    PriceRange,
    RestaurantTier,
    UserRole,
)
from app.models.menu import MenuCategory, MenuItem, MenuItemVariant, MenuSubcategory
from app.models.restaurant import Cuisine, Photo, Restaurant, RestaurantCuisine
from app.models.user import User
from app.services.geocoding import GeocodingService
from app.services.photos import PhotoCapExceeded, PhotoService


# ---- Slug helper ----
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _to_slug(name: str) -> str:
    base = _SLUG_RE.sub("-", name.lower()).strip("-")[:50]
    suffix = uuid.uuid4().hex[:6]
    return f"{base}-{suffix}" if base else suffix


async def _unique_slug(db: AsyncSession, name: str) -> str:
    """Return a slug that doesn't collide with an existing Restaurant.slug."""
    candidate = _to_slug(name)
    for _ in range(5):  # bounded loop; collisions are astronomical but be safe
        existing = await db.scalar(select(Restaurant.id).where(Restaurant.slug == candidate))
        if existing is None:
            return candidate
        candidate = _to_slug(name)
    raise HTTPException(status_code=500, detail="could not generate unique slug; retry")


# ---- Helpers ----
async def get_or_404(db: AsyncSession, restaurant_id: uuid.UUID) -> Restaurant:
    r = await db.get(Restaurant, restaurant_id)
    if r is None:
        raise HTTPException(status_code=404, detail="restaurant not found")
    return r


def _ensure_owner_or_admin(restaurant: Restaurant, actor: User, *, is_admin: bool) -> None:
    if is_admin:
        return
    if restaurant.owner_id != actor.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="you do not own this restaurant",
        )


# ---- tsvector sync ----
async def _update_search_vector(db: AsyncSession, r: Restaurant) -> None:
    """Rebuild the tsvector for a restaurant from name + description.

    Per BRD §5.2, we use PostgreSQL's built-in tsvector (NOT Elasticsearch).
    setweight: A for name (higher boost), B for description.
    """
    await db.execute(
        text("""
            UPDATE restaurants SET search_vector =
              setweight(to_tsvector('english', coalesce(:name, '')), 'A') ||
              setweight(to_tsvector('english', coalesce(:description, '')), 'B')
            WHERE id = :id
        """),
        {"name": r.name, "description": r.description or "", "id": r.id},
    )


# ---- Restaurant CRUD ----
@dataclass
class CreateRestaurantInput:
    name: str
    description: Optional[str]
    address_line: str
    city: str
    state: str
    postal_code: str
    country: str
    price_range: PriceRange
    phone: Optional[str]
    website: Optional[str]
    email: Optional[str]
    cuisine_slugs: list[str]


async def create_restaurant(
    db: AsyncSession,
    *,
    owner: User,
    inp: CreateRestaurantInput,
    geocoder: GeocodingService,
) -> Restaurant:
    if owner.role not in (UserRole.RESTAURANT_OWNER, UserRole.PLATFORM_ADMIN):
        raise HTTPException(
            status_code=403, detail="only Restaurant Owners or Admins can create restaurants",
        )

    slug = await _unique_slug(db, inp.name)
    full_addr = f"{inp.address_line}, {inp.city}, {inp.state} {inp.postal_code}, {inp.country}"
    geo = geocoder.geocode(full_addr)

    r = Restaurant(
        id=uuid.uuid4(),
        owner_id=owner.id,
        name=inp.name,
        slug=slug,
        description=inp.description,
        address_line=inp.address_line,
        city=inp.city,
        state=inp.state,
        postal_code=inp.postal_code,
        country=inp.country,
        latitude=geo.latitude if geo else None,
        longitude=geo.longitude if geo else None,
        geocoded_at=datetime.now(timezone.utc) if geo else None,
        google_place_id=geo.place_id if geo else None,
        phone=inp.phone,
        website=inp.website,
        email=inp.email,
        price_range=inp.price_range,
        tier=RestaurantTier.FREE,
        halal_status=HalalStatus.UNVERIFIED,
        halal_verification_source=HalalVerificationSource.SELF_REPORTED,
    )
    db.add(r)
    await db.flush()

    if inp.cuisine_slugs:
        cuisines = (
            await db.execute(select(Cuisine).where(Cuisine.slug.in_(inp.cuisine_slugs)))
        ).scalars().all()
        found = {c.slug for c in cuisines}
        missing = set(inp.cuisine_slugs) - found
        if missing:
            raise HTTPException(
                status_code=400, detail=f"unknown cuisine slug(s): {sorted(missing)}"
            )
        for c in cuisines:
            db.add(RestaurantCuisine(restaurant_id=r.id, cuisine_id=c.id))

    await _update_search_vector(db, r)
    await db.commit()
    await db.refresh(r)
    return r


async def update_restaurant(
    db: AsyncSession,
    *,
    restaurant: Restaurant,
    actor: User,
    is_admin: bool,
    fields: dict,
) -> Restaurant:
    _ensure_owner_or_admin(restaurant, actor, is_admin=is_admin)

    updatable = {
        "name", "description", "address_line", "city", "state", "postal_code", "country",
        "phone", "website", "email", "price_range",
    }
    for k, v in fields.items():
        if v is not None and k in updatable:
            setattr(restaurant, k, v)
    # Slug stays stable on update — don't re-generate, URLs would break.
    await _update_search_vector(db, restaurant)
    await db.commit()
    await db.refresh(restaurant)
    return restaurant


async def deactivate_restaurant(
    db: AsyncSession, *, restaurant: Restaurant, actor: User, is_admin: bool
) -> Restaurant:
    """Soft-delete: set is_active=False. Per BRD, Admin can deactivate any; Owner can only deactivate their own."""
    _ensure_owner_or_admin(restaurant, actor, is_admin=is_admin)
    restaurant.is_active = False
    await db.commit()
    await db.refresh(restaurant)
    return restaurant


# ---- Photo management (tier cap) ----
async def add_photo(
    db: AsyncSession,
    *,
    restaurant: Restaurant,
    actor: User,
    is_admin: bool,
    data: bytes,
    content_type: str,
    photo_service: PhotoService,
    caption: Optional[str] = None,
) -> Photo:
    _ensure_owner_or_admin(restaurant, actor, is_admin=is_admin)

    cap = PhotoService.cap_for_tier(restaurant.tier)
    current = await db.scalar(
        select(func.count()).select_from(Photo).where(Photo.restaurant_id == restaurant.id)
    )
    if current >= cap:
        raise PhotoCapExceeded(current, cap, restaurant.tier)

    photo = await photo_service.upload(
        restaurant=restaurant, data=data, content_type=content_type, caption=caption,
    )
    db.add(photo)
    await db.commit()
    await db.refresh(photo)
    return photo


# ---- Admin-only: tier + halal status changes ----
async def set_tier(
    db: AsyncSession, *, restaurant: Restaurant, new_tier: RestaurantTier
) -> Restaurant:
    """BRD §3.4: 'Tier changes are managed by Platform Admin.'"""
    restaurant.tier = new_tier
    await db.commit()
    await db.refresh(restaurant)
    return restaurant


async def verify_halal(
    db: AsyncSession,
    *,
    restaurant: Restaurant,
    new_status: HalalStatus,
    admin: User,
) -> Restaurant:
    """BRD §3.2: 'Platform Admin reviews the claim' for self-reported; certifies for body-issued."""
    if new_status in (HalalStatus.VERIFIED, HalalStatus.REVOKED):
        restaurant.halal_status = new_status
        restaurant.halal_verified_at = datetime.now(timezone.utc)
        restaurant.halal_verified_by_admin_id = admin.id
    else:
        restaurant.halal_status = new_status
        restaurant.halal_verified_at = None
        restaurant.halal_verified_by_admin_id = None
    await db.commit()
    await db.refresh(restaurant)
    return restaurant


# ---- Search ----
EARTH_RADIUS_KM = 6371.0088


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two lat/lng pairs."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass
class SearchResult:
    id: uuid.UUID
    slug: str
    name: str
    description: Optional[str]
    city: str
    price_range: PriceRange
    tier: RestaurantTier
    halal_status: HalalStatus
    halal_verification_source: HalalVerificationSource
    latitude: Optional[float]
    longitude: Optional[float]
    distance_km: Optional[float]
    cuisines: list[str]
    rank: Optional[float]  # tsvector rank, lower = better


async def search_restaurants(
    db: AsyncSession,
    *,
    q: Optional[str] = None,
    cuisine_slug: Optional[str] = None,
    min_price: Optional[PriceRange] = None,
    max_price: Optional[PriceRange] = None,
    halal_status: Optional[HalalStatus] = None,
    halal_source: Optional[HalalVerificationSource] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius_km: Optional[float] = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[SearchResult], int]:
    """Search restaurants with full-text + filters + distance.

    Returns (rows, total_count). Each row includes `distance_km` if lat/lng
    + radius_km were provided, else None. `rank` is the tsvector rank if a
    text query was provided, else None.
    """
    where_clauses: list[str] = ["r.is_active = true"]
    params: dict = {"limit": limit, "offset": offset}

    if q:
        # Full-text search via tsvector. plainto_tsquery handles raw user input safely.
        where_clauses.append("r.search_vector @@ plainto_tsquery('english', :q)")
        params["q"] = q
        select_cols = (
            "r.id, r.slug, r.name, r.description, r.city, r.price_range, r.tier, "
            "r.halal_status, r.halal_verification_source, r.latitude, r.longitude, "
            "ts_rank(r.search_vector, plainto_tsquery('english', :q)) AS rank"
        )
        order_by = "rank DESC, r.name ASC"
    else:
        select_cols = (
            "r.id, r.slug, r.name, r.description, r.city, r.price_range, r.tier, "
            "r.halal_status, r.halal_verification_source, r.latitude, r.longitude, "
            "NULL::float4 AS rank"
        )
        order_by = "r.name ASC"

    if cuisine_slug:
        where_clauses.append(
            "r.id IN (SELECT restaurant_id FROM restaurant_cuisines rc "
            "JOIN cuisines c ON c.id = rc.cuisine_id WHERE c.slug = :cuisine_slug)"
        )
        params["cuisine_slug"] = cuisine_slug

    if min_price is not None:
        where_clauses.append("r.price_range >= :min_price")
        params["min_price"] = min_price.value
    if max_price is not None:
        where_clauses.append("r.price_range <= :max_price")
        params["max_price"] = max_price.value

    if halal_status is not None:
        where_clauses.append("r.halal_status = :halal_status")
        params["halal_status"] = halal_status.value
    if halal_source is not None:
        where_clauses.append("r.halal_verification_source = :halal_source")
        params["halal_source"] = halal_source.value

    distance_select = "NULL::float8 AS distance_km"
    if lat is not None and lng is not None and radius_km is not None:
        # Haversine in SQL: 2 * R * asin(sqrt(...)) — returns balanced expression
        # with no outer parens. Caller wraps it for SELECT or WHERE as needed.
        haversine_expr = (
            f"2 * {EARTH_RADIUS_KM} * asin(sqrt("
            "power(sin(radians((r.latitude - :lat) / 2)), 2) + "
            "cos(radians(:lat)) * cos(radians(r.latitude)) * "
            "power(sin(radians((r.longitude - :lng) / 2)), 2)"
            "))"
        )
        distance_select = f"({haversine_expr}) AS distance_km"
        params["lat"] = lat
        params["lng"] = lng
        where_clauses.append("r.latitude IS NOT NULL AND r.longitude IS NOT NULL")
        where_clauses.append(f"({haversine_expr}) <= :radius_km")
        params["radius_km"] = radius_km
        if q is None:
            order_by = "distance_km ASC, r.name ASC"

    where_sql = " AND ".join(where_clauses)
    sql = f"""
        SELECT {select_cols}, {distance_select}
        FROM restaurants r
        WHERE {where_sql}
        ORDER BY {order_by}
        LIMIT :limit OFFSET :offset
    """
    count_sql = f"SELECT COUNT(*) FROM restaurants r WHERE {where_sql}"

    rows = (await db.execute(text(sql), params)).all()
    total = (await db.scalar(text(count_sql), params)) or 0

    results: list[SearchResult] = []
    for row in rows:
        # cuisines: small follow-up query
        cuisines = (
            await db.execute(
                text(
                    "SELECT c.slug FROM cuisines c "
                    "JOIN restaurant_cuisines rc ON rc.cuisine_id = c.id "
                    "WHERE rc.restaurant_id = :rid ORDER BY c.name"
                ),
                {"rid": row.id},
            )
        ).scalars().all()

        results.append(SearchResult(
            id=row.id,
            slug=row.slug,
            name=row.name,
            description=row.description,
            city=row.city,
            price_range=PriceRange(row.price_range),
            tier=RestaurantTier(row.tier),
            halal_status=HalalStatus(row.halal_status),
            halal_verification_source=HalalVerificationSource(row.halal_verification_source),
            latitude=float(row.latitude) if row.latitude is not None else None,
            longitude=float(row.longitude) if row.longitude is not None else None,
            distance_km=float(row.distance_km) if row.distance_km is not None else None,
            cuisines=list(cuisines),
            rank=float(row.rank) if row.rank is not None else None,
        ))
    return results, total


# ---- Profile aggregation ----
@dataclass
class RestaurantProfile:
    """Per BRD §3.1: photos, menu, halal verification badge, reviews, active deals."""
    restaurant: Restaurant
    cuisines: list[str]
    photos: list[Photo]
    menu: list[dict]  # 4-level: category → subcategory → item → variant
    halal_badge: dict
    aggregate_rating: Optional[float]  # None until Stage 5 (Reviews) lands
    active_deals: list  # [] until Stage 6 (Deals) lands


async def build_profile(db: AsyncSession, restaurant_id: uuid.UUID) -> RestaurantProfile:
    r = await get_or_404(db, restaurant_id)
    cuisines = (
        await db.execute(
            text(
                "SELECT c.slug FROM cuisines c "
                "JOIN restaurant_cuisines rc ON rc.cuisine_id = c.id "
                "WHERE rc.restaurant_id = :rid ORDER BY c.name"
            ),
            {"rid": r.id},
        )
    ).scalars().all()

    photos = (
        await db.execute(
            select(Photo).where(Photo.restaurant_id == r.id).order_by(Photo.sort_order)
        )
    ).scalars().all()

    # Build 4-level menu tree: category → subcategory → item → variant
    categories = (
        await db.execute(
            select(MenuCategory)
            .where(MenuCategory.restaurant_id == r.id)
            .order_by(MenuCategory.sort_order)
        )
    ).scalars().all()

    menu_out: list[dict] = []
    for cat in categories:
        subcats = (
            await db.execute(
                select(MenuSubcategory)
                .where(MenuSubcategory.category_id == cat.id)
                .order_by(MenuSubcategory.sort_order)
            )
        ).scalars().all()

        cat_items = (
            await db.execute(
                select(MenuItem)
                .where(MenuItem.category_id == cat.id)
                .order_by(MenuItem.sort_order)
            )
        ).scalars().all()

        # Items directly in this category (no subcategory)
        direct_items = [i for i in cat_items if i.subcategory_id is None]
        # Subcategory index
        items_by_sub: dict[uuid.UUID, list[MenuItem]] = {}
        for it in cat_items:
            if it.subcategory_id is not None:
                items_by_sub.setdefault(it.subcategory_id, []).append(it)

        def _item_dict(it: MenuItem) -> dict:
            variants = [
                {
                    "id": str(v.id),
                    "name": v.name,
                    "price_cents": v.price_cents,
                    "is_default": v.is_default,
                    "is_available": v.is_available,
                    "sort_order": v.sort_order,
                }
                for v in it.variants
            ]
            return {
                "id": str(it.id),
                "name": it.name,
                "description": it.description,
                "base_price_cents": it.base_price_cents,
                "currency": it.currency,
                "photo_url": it.photo_url,
                "allergens": it.allergens or [],
                "calories": it.calories,
                "prep_time_minutes": it.prep_time_minutes,
                "is_available": it.is_available,
                "sort_order": it.sort_order,
                "variants": variants,
            }

        cat_dict: dict = {
            "id": str(cat.id),
            "name": cat.name,
            "sort_order": cat.sort_order,
            "is_active": cat.is_active,
            "subcategories": [
                {
                    "id": str(sc.id),
                    "name": sc.name,
                    "sort_order": sc.sort_order,
                    "is_active": sc.is_active,
                    "items": [_item_dict(it) for it in items_by_sub.get(sc.id, [])],
                }
                for sc in subcats
            ],
            "items": [_item_dict(it) for it in direct_items],
        }
        menu_out.append(cat_dict)

    return RestaurantProfile(
        restaurant=r,
        cuisines=list(cuisines),
        photos=list(photos),
        menu=menu_out,
        halal_badge={
            "status": r.halal_status.value,
            "source": r.halal_verification_source.value,
            "verified_at": r.halal_verified_at.isoformat() if r.halal_verified_at else None,
        },
        aggregate_rating=await _aggregate_rating(db, r.id),
        active_deals=[],        # populated in Stage 6
    )


async def _aggregate_rating(db: AsyncSession, restaurant_id: uuid.UUID) -> Optional[float]:
    """Stage 5: average rating over APPROVED reviews only. Returns None
    if no approved reviews yet. Pending + rejected are excluded per BRD §3.3.
    """
    from app.models.enums import ReviewStatus
    from app.models.review import Review
    from sqlalchemy import func
    row = (
        await db.execute(
            select(func.avg(Review.rating)).where(
                Review.restaurant_id == restaurant_id,
                Review.moderation_status == ReviewStatus.APPROVED.value,
            )
        )
    ).one()
    avg = row[0]
    return float(avg) if avg is not None else None
