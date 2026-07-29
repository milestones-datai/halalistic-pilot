"""Discovery, reviews, deals, account — Stage 11 consumer routes.

All under the same signed-cookie session. RBAC via the helpers in
`app.web.deps` — diners and owners can both use these routes.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.deal import Deal, DealAudience, DealStatus
from app.models.enums import RestaurantTier, ReviewStatus, UserRole
from app.models.billing import UserBillingSubscription
from app.models.review import Review
from app.models.user import User
from app.services import deals as deal_service
from app.services import reviews as review_service
from app.services import points as points_service
from app.services import gift_cards as gift_card_service
from app.services import billing as billing_service
from app.services import certificates as halal_service
from app.services import tags as tag_service
from app.web.deps import get_optional_user, require_consumer_role
from app.web.templates_env import render

router = APIRouter(tags=["web-discovery"])


async def _render(request: Request, template: str, user: Optional[User], **ctx) -> HTMLResponse:
    flash = request.session.pop("flash", None) if hasattr(request, "session") else None
    return HTMLResponse(render(template, user=user, flash=flash, app_version="0.11.0",
                              settings=settings, **ctx))


class _DV:
    """Tiny view-model for deals_list so the template can render
    restaurant_name / restaurant_city alongside the deal row (one SQL
    query, joined)."""
    __slots__ = ("id", "title", "description", "deal_type", "end_date",
                 "restaurant_id", "restaurant_name", "restaurant_city")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _set_flash(request: Request, kind: str, text: str) -> None:
    request.session["flash"] = {"kind": kind, "text": text}


# ---------- restaurants: list / search / profile ----------
@router.get("/restaurants", response_class=HTMLResponse, response_model=None)
async def restaurants_list(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str = "",
    halal_status: str = "",
    cuisine: str = "",
    sort: str = "name",
) -> HTMLResponse:
    from app.models.restaurant import Restaurant
    stmt = select(Restaurant).where(Restaurant.is_active.is_(True))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            Restaurant.name.ilike(like),
            Restaurant.description.ilike(like),
            Restaurant.city.ilike(like),
            Restaurant.address_line.ilike(like),
        ))
    if halal_status:
        stmt = stmt.where(Restaurant.halal_status == halal_status)
    if cuisine:
        stmt = stmt.where(Restaurant.cuisines.any(name=cuisine))
    if sort == "newest":
        stmt = stmt.order_by(Restaurant.created_at.desc())
    elif sort == "tier":
        stmt = stmt.order_by(Restaurant.tier.desc(), Restaurant.name)
    else:
        stmt = stmt.order_by(Restaurant.name)
    rows = list((await db.execute(stmt.limit(60))).scalars().all())
    user = await get_optional_user(request, db)
    return await _render(
        request, "restaurants_list.html", user,
        restaurants=rows, q=q, halal_status=halal_status, sort=sort,
    )


@router.get("/restaurants/{rid}", response_class=HTMLResponse, response_model=None)
async def restaurant_detail(
    request: Request,
    rid: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    from app.models.restaurant import Restaurant
    from app.models.menu import MenuCategory
    r = await db.get(Restaurant, rid)
    if r is None:
        raise HTTPException(status_code=404, detail="restaurant not found")
    owner = await db.get(User, r.owner_id) if r.owner_id else None
    # Approved reviews + reviewer display name
    reviews_rows = list((await db.execute(
        select(Review).where(
            Review.restaurant_id == rid,
            Review.moderation_status == ReviewStatus.APPROVED.value,
        ).order_by(Review.created_at.desc()).limit(20)
    )).scalars().all())
    reviewer_ids = {rv.reviewer_id for rv in reviews_rows}
    rmap = {}
    if reviewer_ids:
        for u in list((await db.execute(
            select(User).where(User.id.in_(reviewer_ids))
        )).scalars().all()):
            rmap[u.id] = u.display_name
    # Active deals
    today = date.today()
    deals = list((await db.execute(
        select(Deal).where(
            Deal.restaurant_id == rid,
            Deal.status == DealStatus.APPROVED.value,
            Deal.start_date <= today, Deal.end_date >= today,
        ).order_by(Deal.created_at.desc())
    )).scalars().all())
    # Halal cert summary
    cert = await halal_service.get_active_cert(db, rid)
    # Menu (1 level deep for MVP)
    menu = list((await db.execute(
        select(MenuCategory).where(MenuCategory.restaurant_id == rid)
        .order_by(MenuCategory.sort_order)
    )).scalars().all())
    user = await get_optional_user(request, db)
    # Hydrate tags for the review form (needs user to be a diner)
    tag_list = list((await db.execute(
        select(__import__("app.models.tag", fromlist=["Tag"]).Tag).order_by("name")
    )).scalars().all()) if user else []
    return await _render(
        request, "restaurant_detail.html", user,
        restaurant=r, owner_email=owner.email if owner else "—",
        reviews=reviews_rows, reviewer_names=rmap, deals=deals, cert=cert,
        menu=menu, tags=tag_list,
    )


@router.post("/restaurants/{rid}/review", response_model=None)
async def review_submit(
    request: Request,
    rid: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_consumer_role(UserRole.DINER, UserRole.PLATFORM_ADMIN))],
    rating: int = Form(..., ge=1, le=5),
    body: str = Form(..., min_length=1, max_length=4000),
    tag_ids: str = Form(default=""),
    instagram_embed_url: str = Form(default=""),
    file0: Optional[UploadFile] = File(default=None),
    file1: Optional[UploadFile] = File(default=None),
    file2: Optional[UploadFile] = File(default=None),
) -> Response:
    import json as _json
    from app.models.restaurant import Restaurant
    r = await db.get(Restaurant, rid)
    if r is None:
        raise HTTPException(status_code=404, detail="restaurant not found")
    tag_id_list: list[int] = []
    if tag_ids:
        try:
            parsed = _json.loads(tag_ids)
            if isinstance(parsed, list) and all(isinstance(t, int) for t in parsed):
                tag_id_list = parsed
        except Exception:
            tag_id_list = []
    uploads = []
    for f in (file0, file1, file2):
        if f is None:
            continue
        data = await f.read()
        if not data:
            continue
        uploads.append((data, f.content_type or "image/jpeg"))
    if len(uploads) > 3:
        uploads = uploads[:3]
    try:
        await review_service.create_review(
            db, restaurant_id=rid, reviewer=user, rating=rating, body=body,
            tag_ids=tag_id_list, photo_uploads=uploads,
            instagram_embed_url=instagram_embed_url or None,
        )
    except HTTPException as exc:
        _set_flash(request, "error", str(exc.detail))
        return RedirectResponse(url=f"/restaurants/{rid}", status_code=303)
    _set_flash(request, "success", "Thanks! Your review is in moderation and will appear after admin approval.")
    return RedirectResponse(url=f"/restaurants/{rid}", status_code=303)


# ---------- deals ----------
@router.get("/deals", response_class=HTMLResponse, response_model=None)
async def deals_list(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    city: str = "",
    deal_type: str = "",
) -> HTMLResponse:
    from app.models.restaurant import Restaurant
    today = date.today()
    stmt = (
        select(Deal, Restaurant)
        .join(Restaurant, Restaurant.id == Deal.restaurant_id)
        .where(
            Deal.status == DealStatus.APPROVED.value,
            Deal.start_date <= today, Deal.end_date >= today,
            Deal.target_audience == DealAudience.PUBLIC.value,
            Restaurant.is_active.is_(True),
        )
    )
    if city:
        stmt = stmt.where(Restaurant.city.ilike(f"%{city}%"))
    if deal_type:
        stmt = stmt.where(Deal.deal_type == deal_type)
    stmt = stmt.order_by(Deal.created_at.desc()).limit(60)
    rows = list((await db.execute(stmt)).all())
    deals_view = [
        _DV(
            id=d.id, title=d.title, description=d.description,
            deal_type=d.deal_type, end_date=d.end_date,
            restaurant_id=d.restaurant_id, restaurant_name=r.name,
            restaurant_city=r.city,
        ) for d, r in rows
    ]
    user = await get_optional_user(request, db)
    return await _render(
        request, "deals_list.html", user,
        deals=deals_view, city=city, deal_type=deal_type,
    )


# ---------- diner account ----------
@router.get("/account", response_class=HTMLResponse, response_model=None)
async def account_dashboard(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_consumer_role(UserRole.DINER, UserRole.RESTAURANT_OWNER, UserRole.PLATFORM_ADMIN))],
) -> HTMLResponse:
    # Points balance (canonical re-sync from ledger)
    points = await points_service.get_balance(db, user.id)
    # Referral code + link
    from app.services import referrals as referrals_service
    code = await referrals_service.get_or_create_referral_code(db, user)
    ref_link = f"{settings.app_public_url.rstrip('/')}/web/signup?ref={code}"
    # Active subscription (if any)
    sub = (await db.execute(
        select(UserBillingSubscription).where(
            UserBillingSubscription.user_id == user.id,
            UserBillingSubscription.status.in_(("active", "trialing")),
        ).order_by(UserBillingSubscription.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    # Pending gift card redemptions
    from app.models.points import GiftCardRedemption
    reds = list((await db.execute(
        select(GiftCardRedemption).where(GiftCardRedemption.user_id == user.id)
        .order_by(GiftCardRedemption.created_at.desc()).limit(5)
    )).scalars().all())
    # Email-verified status
    return await _render(
        request, "account_dashboard.html", user,
        points=points, referral_code=code, referral_link=ref_link,
        subscription=sub, redemptions=reds,
        points_min=settings.points_values.get("min_redemption", 1000),
    )


@router.post("/account/redeem", response_model=None)
async def account_redeem(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_consumer_role(UserRole.DINER, UserRole.RESTAURANT_OWNER, UserRole.PLATFORM_ADMIN))],
    points_to_spend: int = Form(..., ge=100),
    delivery_email: str = Form(...),
) -> Response:
    try:
        await gift_card_service.request_redemption(
            db, user_id=user.id, points=points_to_spend, email=delivery_email,
        )
    except (HTTPException, ValueError) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        _set_flash(request, "error", detail)
        return RedirectResponse(url="/account", status_code=303)
    _set_flash(request, "success", "Redemption submitted! We'll email your gift card code within 1-2 business days.")
    return RedirectResponse(url="/account", status_code=303)


@router.post("/account/subscribe", response_model=None)
async def account_subscribe(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_consumer_role(UserRole.DINER, UserRole.RESTAURANT_OWNER, UserRole.PLATFORM_ADMIN))],
) -> Response:
    # Create a Stripe Checkout session and redirect.
    try:
        url = await billing_service.create_user_checkout(
            db, user=user, success_url=f"{settings.app_public_url}/account?subscribed=1",
            cancel_url=f"{settings.app_public_url}/account?canceled=1",
        )
    except HTTPException as exc:
        _set_flash(request, "error", f"Could not start checkout: {exc.detail}")
        return RedirectResponse(url="/account", status_code=303)
    return RedirectResponse(url=url, status_code=303)
