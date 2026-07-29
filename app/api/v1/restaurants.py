"""/restaurants/* + /search/* routers.

Public (unauthenticated) browse/search/profile + owner-only CRUD + admin override.
Photo upload is multipart/form-data on the owner endpoint.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.deps.auth import get_current_user, require_role
from app.db.session import get_db
from app.models.enums import (
    HalalStatus,
    HalalVerificationSource,
    PriceRange,
    RestaurantTier,
    UserRole,
)
from app.models.user import User
from app.services import restaurant_service
from app.services.certificates import (
    CertificateService,
    CertifyingBodyNotFound,
    InvalidDateRange,
    CertificateError as CertServiceError,
)
from app.services.geocoding import GeocodingService
from app.services.photos import PhotoCapExceeded, PhotoService

router = APIRouter(tags=["restaurants"])
search_router = APIRouter(prefix="/search", tags=["search"])


# ---- Schemas ----
class CreateRestaurantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    address_line: str = Field(min_length=1, max_length=255)
    city: str = Field(default="Houston", max_length=100)
    state: str = Field(default="TX", max_length=50)
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(default="US", max_length=50)
    price_range: PriceRange = PriceRange.MODERATE
    phone: Optional[str] = Field(default=None, max_length=30)
    website: Optional[str] = Field(default=None, max_length=255)
    email: Optional[EmailStr] = None
    cuisine_slugs: list[str] = Field(default_factory=list)


class UpdateRestaurantIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    address_line: Optional[str] = Field(default=None, min_length=1, max_length=255)
    city: Optional[str] = Field(default=None, max_length=100)
    state: Optional[str] = Field(default=None, max_length=50)
    postal_code: Optional[str] = Field(default=None, max_length=20)
    country: Optional[str] = Field(default=None, max_length=50)
    price_range: Optional[PriceRange] = None
    phone: Optional[str] = Field(default=None, max_length=30)
    website: Optional[str] = Field(default=None, max_length=255)
    email: Optional[EmailStr] = None


class RestaurantOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: Optional[str]
    address_line: str
    city: str
    state: str
    postal_code: str
    country: str
    latitude: Optional[float]
    longitude: Optional[float]
    price_range: str
    tier: str
    halal_status: str
    halal_verification_source: str
    is_active: bool
    cuisines: list[str]
    photo_count: int
    photo_cap: int


class PhotoOut(BaseModel):
    id: uuid.UUID
    blob_url: str
    caption: Optional[str]
    sort_order: int
    width: Optional[int]
    height: Optional[int]


class ProfileOut(BaseModel):
    restaurant: dict
    cuisines: list[str]
    photos: list[dict]
    menu: list[dict]
    halal_badge: dict
    aggregate_rating: Optional[float]
    review_count: int
    approved_reviews: list[dict]
    active_deals: list


def _to_restaurant_out(r, cuisines: list[str], photo_count: int) -> RestaurantOut:
    return RestaurantOut(
        id=r.id,
        slug=r.slug,
        name=r.name,
        description=r.description,
        address_line=r.address_line,
        city=r.city,
        state=r.state,
        postal_code=r.postal_code,
        country=r.country,
        latitude=float(r.latitude) if r.latitude is not None else None,
        longitude=float(r.longitude) if r.longitude is not None else None,
        price_range=r.price_range.value,
        tier=r.tier.value,
        halal_status=r.halal_status.value,
        halal_verification_source=r.halal_verification_source.value,
        is_active=r.is_active,
        cuisines=cuisines,
        photo_count=photo_count,
        photo_cap=PhotoService.cap_for_tier(r.tier),
    )


# ---- Public GET: browse one / browse all ----
@router.get("/restaurants/{restaurant_id}", response_model=ProfileOut)
async def get_restaurant_profile(
    restaurant_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProfileOut:
    from app.services import deals as deal_service
    from app.services import reviews as review_service
    profile = await restaurant_service.build_profile(db, restaurant_id)
    # Pull the approved review list for the same restaurant. We do it after
    # build_profile (which already loaded the restaurant) so we don't load
    # it twice — and we get the most-recent first.
    approved = await review_service.list_approved_for_restaurant(
        db, restaurant_id=restaurant_id, limit=20, offset=0,
    )
    approved_payload = [
        {
            "id": str(r.id),
            "rating": r.rating,
            "body": r.body,
            "instagram_embed_url": r.instagram_embed_url,
            "created_at": r.created_at.isoformat(),
            "tags": [{"id": t.id, "name": t.name, "slug": t.slug} for t in r.tags],
            "photos": [
                {"id": str(p.id), "blob_url": p.blob_url,
                 "content_type": p.content_type, "size_bytes": p.size_bytes,
                 "sort_order": p.sort_order}
                for p in r.photos
            ],
            "reviewer": {
                "id": str(r.reviewer_id),
                "display_name": r.reviewer.display_name if r.reviewer is not None else "(deleted)",
            },
        }
        for r in approved
    ]
    # Active deals (Stage 6): approved AND end_date >= today. Public-audience
    # only because the public detail endpoint is unauthenticated.
    active_deals = await deal_service.list_active_for_restaurant(
        db, restaurant_id=restaurant_id, viewer_id=None,
    )
    active_deals_payload = [
        {
            "id": str(d.id),
            "title": d.title,
            "description": d.description,
            "deal_type": d.deal_type.value if hasattr(d.deal_type, "value") else str(d.deal_type),
            "discount_value": float(d.discount_value) if d.discount_value is not None else None,
            "start_date": d.start_date.isoformat(),
            "end_date": d.end_date.isoformat(),
            "target_audience": d.target_audience.value if hasattr(d.target_audience, "value") else str(d.target_audience),
        }
        for d in active_deals
    ]
    return ProfileOut(
        restaurant={
            "id": str(profile.restaurant.id),
            "slug": profile.restaurant.slug,
            "name": profile.restaurant.name,
            "description": profile.restaurant.description,
            "address_line": profile.restaurant.address_line,
            "city": profile.restaurant.city,
            "state": profile.restaurant.state,
            "postal_code": profile.restaurant.postal_code,
            "country": profile.restaurant.country,
            "latitude": float(profile.restaurant.latitude) if profile.restaurant.latitude is not None else None,
            "longitude": float(profile.restaurant.longitude) if profile.restaurant.longitude is not None else None,
            "price_range": profile.restaurant.price_range.value,
            "tier": profile.restaurant.tier.value,
            "halal_status": profile.restaurant.halal_status.value,
            "halal_verification_source": profile.restaurant.halal_verification_source.value,
        },
        cuisines=profile.cuisines,
        photos=[{"id": str(p.id), "blob_url": p.blob_url, "caption": p.caption,
                 "sort_order": p.sort_order, "width": p.width, "height": p.height} for p in profile.photos],
        menu=profile.menu,
        halal_badge=profile.halal_badge,
        aggregate_rating=profile.aggregate_rating,
        review_count=len(approved_payload),
        approved_reviews=approved_payload,
        active_deals=active_deals_payload,
    )


# ---- Search (public) ----
@search_router.get("/restaurants")
async def search(
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Optional[str] = Query(default=None, max_length=200),
    cuisine: Optional[str] = Query(default=None),
    min_price: Optional[PriceRange] = Query(default=None),
    max_price: Optional[PriceRange] = Query(default=None),
    halal_status: Optional[HalalStatus] = Query(default=None),
    halal_source: Optional[HalalVerificationSource] = Query(default=None),
    lat: Optional[float] = Query(default=None, ge=-90, le=90),
    lng: Optional[float] = Query(default=None, ge=-180, le=180),
    radius_km: Optional[float] = Query(default=None, gt=0, le=50),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows, total = await restaurant_service.search_restaurants(
        db,
        q=q,
        cuisine_slug=cuisine,
        min_price=min_price,
        max_price=max_price,
        halal_status=halal_status,
        halal_source=halal_source,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        limit=limit,
        offset=offset,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [
            {
                "id": str(r.id),
                "slug": r.slug,
                "name": r.name,
                "description": r.description,
                "city": r.city,
                "price_range": r.price_range.value,
                "tier": r.tier.value,
                "halal_status": r.halal_status.value,
                "halal_verification_source": r.halal_verification_source.value,
                "latitude": r.latitude,
                "longitude": r.longitude,
                "distance_km": r.distance_km,
                "cuisines": r.cuisines,
                "rank": r.rank,
            }
            for r in rows
        ],
    }


# ---- Owner: create / update / deactivate ----
@router.post(
    "/restaurants",
    response_model=RestaurantOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_my_restaurant(
    body: CreateRestaurantIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    owner: Annotated[User, Depends(require_role(UserRole.RESTAURANT_OWNER, UserRole.PLATFORM_ADMIN))],
) -> RestaurantOut:
    r = await restaurant_service.create_restaurant(
        db,
        owner=owner,
        inp=restaurant_service.CreateRestaurantInput(
            name=body.name,
            description=body.description,
            address_line=body.address_line,
            city=body.city,
            state=body.state,
            postal_code=body.postal_code,
            country=body.country,
            price_range=body.price_range,
            phone=body.phone,
            website=body.website,
            email=body.email,
            cuisine_slugs=body.cuisine_slugs,
        ),
        geocoder=GeocodingService(),
    )
    return _to_restaurant_out(r, r.cuisines, len(r.photos))


@router.put("/restaurants/{restaurant_id}", response_model=RestaurantOut)
async def update_my_restaurant(
    restaurant_id: uuid.UUID,
    body: UpdateRestaurantIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> RestaurantOut:
    r = await restaurant_service.get_or_404(db, restaurant_id)
    is_admin = actor.role == UserRole.PLATFORM_ADMIN
    fields = body.model_dump(exclude_unset=True)
    r = await restaurant_service.update_restaurant(
        db, restaurant=r, actor=actor, is_admin=is_admin, fields=fields,
    )
    return _to_restaurant_out(r, r.cuisines, len(r.photos))


@router.delete("/restaurants/{restaurant_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def deactivate_my_restaurant(
    restaurant_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
):
    r = await restaurant_service.get_or_404(db, restaurant_id)
    is_admin = actor.role.value == UserRole.PLATFORM_ADMIN
    await restaurant_service.deactivate_restaurant(
        db, restaurant=r, actor=actor, is_admin=is_admin,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- Owner: photo upload ----
@router.post(
    "/restaurants/{restaurant_id}/photos",
    response_model=PhotoOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_photo(
    restaurant_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
    file: UploadFile = File(...),
    caption: Optional[str] = Form(default=None, max_length=500),
) -> PhotoOut:
    r = await restaurant_service.get_or_404(db, restaurant_id)
    is_admin = actor.role == UserRole.PLATFORM_ADMIN
    data = await file.read()
    try:
        photo = await restaurant_service.add_photo(
            db,
            restaurant=r,
            actor=actor,
            is_admin=is_admin,
            data=data,
            content_type=file.content_type or "image/jpeg",
            photo_service=PhotoService(),
            caption=caption,
        )
    except PhotoCapExceeded as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "photo_cap_exceeded",
                "tier": exc.tier.value,
                "cap": exc.cap,
                "current": exc.current_count,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return PhotoOut(
        id=photo.id,
        blob_url=photo.blob_url,
        caption=photo.caption,
        sort_order=photo.sort_order,
        width=photo.width,
        height=photo.height,
    )


# ---- Admin: tier + halal verification ----
class AdminSetTierIn(BaseModel):
    tier: RestaurantTier


class AdminVerifyHalalIn(BaseModel):
    status: HalalStatus


@router.put(
    "/admin/restaurants/{restaurant_id}/tier",
    response_model=RestaurantOut,
)
async def admin_set_tier(
    restaurant_id: uuid.UUID,
    body: AdminSetTierIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> RestaurantOut:
    r = await restaurant_service.get_or_404(db, restaurant_id)
    r = await restaurant_service.set_tier(db, restaurant=r, new_tier=body.tier)
    return _to_restaurant_out(r, r.cuisines, len(r.photos))


@router.put(
    "/admin/restaurants/{restaurant_id}/halal",
    response_model=RestaurantOut,
)
async def admin_verify_halal(
    restaurant_id: uuid.UUID,
    body: AdminVerifyHalalIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> RestaurantOut:
    r = await restaurant_service.get_or_404(db, restaurant_id)
    r = await restaurant_service.verify_halal(
        db, restaurant=r, new_status=body.status, admin=admin,
    )
    return _to_restaurant_out(r, r.cuisines, len(r.photos))


# ---- Owner: upload halal certificate (Stage 4) ----
class HalalCertOut(BaseModel):
    id: uuid.UUID
    restaurant_id: uuid.UUID
    certifying_body_name: str
    issue_date: str
    expiry_date: Optional[str]
    status: str
    blob_url: str


class OwnerSetSelfReportedIn(BaseModel):
    """Owner marks the restaurant as self-reported halal (no cert).

    BRD §3.2: self-reporting is a first-class path. This puts the restaurant
    into halal_status=pending; an admin must confirm via /admin/restaurants/{id}/halal.
    """
    notes: Optional[str] = Field(default=None, max_length=1000)


@router.post(
    "/restaurants/{restaurant_id}/halal-certificate",
    response_model=HalalCertOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_halal_certificate(
    restaurant_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
    file: UploadFile = File(...),
    certifying_body_id: Optional[int] = Form(default=None),
    custom_certifying_body: Optional[str] = Form(default=None, max_length=200),
    issue_date: str = Form(...),  # ISO date string
    expiry_date: Optional[str] = Form(default=None),
) -> HalalCertOut:
    """Owner uploads a halal certificate.

    Either `certifying_body_id` (from the dropdown) OR `custom_certifying_body`
    (for "Other") must be provided — not both, not neither.
    """
    from datetime import date as _date
    from app.models.certifying_body import CertifyingBody as CB
    from app.models.halal_certificate import HalalCertificate
    from app.models.enums import CertificateStatus
    from sqlalchemy import select as _select

    r = await restaurant_service.get_or_404(db, restaurant_id)
    is_admin = actor.role == UserRole.PLATFORM_ADMIN
    restaurant_service._ensure_owner_or_admin(r, actor, is_admin=is_admin)

    if (certifying_body_id is None) == (custom_certifying_body is None):
        raise HTTPException(
            status_code=400,
            detail="provide exactly one of certifying_body_id or custom_certifying_body",
        )

    body_obj: Optional[CB] = None
    if certifying_body_id is not None:
        body_obj = await db.get(CB, certifying_body_id)
        if body_obj is None or not body_obj.is_active:
            raise HTTPException(status_code=400, detail="unknown or inactive certifying_body_id")

    try:
        issue_d = _date.fromisoformat(issue_date)
        expiry_d = _date.fromisoformat(expiry_date) if expiry_date else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid date: {exc}")

    try:
        CertificateService.validate_dates(issue_d, expiry_d)
    except InvalidDateRange as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Create cert row first to get a UUID for the blob name.
    # We pre-assign id in Python (see model default) so we can use it
    # WITHOUT flushing — the actual INSERT happens in the final commit,
    # by which point blob_name/blob_url/size_bytes are populated.
    cert = HalalCertificate(
        id=uuid.uuid4(),
        restaurant_id=restaurant_id,
        uploaded_by=actor.id,
        certifying_body_id=body_obj.id if body_obj else None,
        custom_certifying_body=custom_certifying_body,
        issue_date=issue_d,
        expiry_date=expiry_d,
        status=CertificateStatus.PENDING,
    )
    db.add(cert)

    data = await file.read()
    content_type = file.content_type or "application/pdf"
    try:
        blob_name, blob_url = await CertificateService().upload(
            cert_id=cert.id, restaurant_id=restaurant_id, data=data, content_type=content_type,
        )
    except CertServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cert.blob_name = blob_name
    cert.blob_url = blob_url
    cert.content_type = content_type
    cert.size_bytes = len(data)

    # Restaurant transitions to PENDING awaiting admin review
    r.halal_status = HalalStatus.PENDING
    await db.commit()
    await db.refresh(cert)
    name = body_obj.name if body_obj else custom_certifying_body
    return HalalCertOut(
        id=cert.id, restaurant_id=cert.restaurant_id, certifying_body_name=name,
        issue_date=cert.issue_date.isoformat(),
        expiry_date=cert.expiry_date.isoformat() if cert.expiry_date else None,
        status=cert.status, blob_url=cert.blob_url,
    )


@router.put(
    "/restaurants/{restaurant_id}/halal-source",
    response_model=RestaurantOut,
)
async def owner_set_self_reported(
    restaurant_id: uuid.UUID,
    body: OwnerSetSelfReportedIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> RestaurantOut:
    """Owner declares the restaurant as self-reported halal (no cert required)."""
    r = await restaurant_service.get_or_404(db, restaurant_id)
    is_admin = actor.role == UserRole.PLATFORM_ADMIN
    restaurant_service._ensure_owner_or_admin(r, actor, is_admin=is_admin)
    r.halal_status = HalalStatus.PENDING
    r.halal_verification_source = HalalVerificationSource.SELF_REPORTED
    # No admin yet — leave halal_verified_at/by_admin_id as NULL
    if body.notes:
        # The notes field doesn't exist on Restaurant yet; for now we just
        # acknowledge via a debug log. A future model revision can add it.
        from app.core.logging import get_logger
        get_logger("halalistic.api").info(
            "owner self-reported claim notes for %s: %s", restaurant_id, body.notes
        )
    await db.commit()
    await db.refresh(r)
    return _to_restaurant_out(r, r.cuisines, len(r.photos))
