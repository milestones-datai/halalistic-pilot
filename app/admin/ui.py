"""Admin UI router — /admin/ui/* (Stage 10).

Internal admin/curator console. Server-rendered Jinja2 + a touch of
HTMX for inline actions. All routes require a PLATFORM_ADMIN or
DEAL_CURATOR session — `require_ui_role` 303-redirects to /admin/ui/login
if not signed in, 403-renders an error page if the wrong role.

This module is intentionally separate from /admin/* (the API surface)
so we can give internal users a different auth model (signed cookies
vs Bearer JWT) and a different timeout policy. The two surfaces
intentionally do NOT share URL prefixes.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.deps import get_current_ui_user, require_ui_role, session_secret, ui_login, ui_logout
from app.admin.templates_env import render_template
from app.core.config import settings
from app.db.session import get_db
from app.models.deal import Deal, DealStatus, DealType, DealAudience
from app.models.enums import CertificateStatus, RestaurantTier, ReviewStatus, UserRole
from app.models.halal_certificate import HalalCertificate
from app.models.review import Review
from app.models.user import User
from app.services import admin_ops
from app.services import certificates as cert_service
from app.services import deals as deal_service
from app.services import reviews as review_service

router = APIRouter(prefix="/admin/ui", include_in_schema=False)


# ---------- helpers ----------
async def _queue_for_sidebar(db: AsyncSession) -> dict:
    """Cheap counts for the sidebar badges. Errors are swallowed so a
    KPI blip never breaks the page render.
    """
    try:
        return await admin_ops.queue_counts(db)
    except Exception:  # noqa: BLE001
        return {"pending_certs": 0, "pending_reviews": 0, "flagged_reviews": 0, "pending_deals": 0}


async def _render(
    request: Request, db: AsyncSession, template: str,
    user: Optional[User], active: str, **context,
) -> HTMLResponse:
    queue = await _queue_for_sidebar(db)
    return HTMLResponse(content=render_template(
        template,
        user=user,
        queue=queue,
        active=active,
        app_version="0.10.0",
        settings=settings,
        **context,
    ))


# ---------- auth ----------
@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_get(request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> HTMLResponse:
    # If already signed in, skip the form.
    if await get_current_ui_user(request, db) is not None:
        return RedirectResponse(url="/admin/ui/dashboard", status_code=303)
    return HTMLResponse(content=render_template("login.html", user=None, active="", app_version="0.10.0"))


@router.post("/login", response_model=None)
async def login_post(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    email: str = Form(...),
    password: str = Form(...),
) -> Response:
    try:
        await ui_login(db, request, email=email.strip().lower(), password=password)
    except HTTPException as exc:
        return HTMLResponse(
            content=render_template("login.html", user=None, active="",
                                     app_version="0.10.0", error=exc.detail),
            status_code=exc.status_code,
        )
    return RedirectResponse(url="/admin/ui/dashboard", status_code=303)


@router.post("/logout", response_model=None)
async def logout(request: Request) -> Response:
    await ui_logout(request)
    return RedirectResponse(url="/admin/ui/login", status_code=303)


# ---------- dashboard ----------
@router.get("", response_class=RedirectResponse, response_model=None)
@router.get("/", response_class=RedirectResponse, response_model=None)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/admin/ui/dashboard", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse, response_model=None)
async def dashboard(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR))],
) -> HTMLResponse:
    kpis = await admin_ops.dashboard_kpis(db)
    return await _render(request, db, "dashboard.html", user, active="dashboard", kpis=kpis)


# ---------- restaurants ----------
@router.get("/restaurants", response_class=HTMLResponse, response_model=None)
async def restaurants_list(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR))],
    pending: int = 0,
) -> HTMLResponse:
    from app.models.restaurant import Restaurant
    if pending:
        # Restaurants that have at least one pending cert.
        rows = list((await db.execute(
            select(Restaurant).join(HalalCertificate, HalalCertificate.restaurant_id == Restaurant.id)
            .where(HalalCertificate.status == CertificateStatus.PENDING.value)
            .distinct().order_by(Restaurant.name)
        )).scalars().all())
    else:
        rows = list((await db.execute(
            select(Restaurant).order_by(Restaurant.created_at.desc()).limit(200)
        )).scalars().all())
    owner_ids = {r.owner_id for r in rows if r.owner_id}
    owner_emails: dict = {}
    if owner_ids:
        owners = list((await db.execute(
            select(User.id, User.email).where(User.id.in_(owner_ids))
        )).all())
        owner_emails = {o.id: o.email for o in owners}
    pending_certs = (await admin_ops.queue_counts(db))["pending_certs"]
    return await _render(
        request, db, "restaurants_list.html", user, active="restaurants",
        restaurants=rows, owner_emails=owner_emails, pending_only=bool(pending),
        pending_certs=pending_certs,
    )


@router.get("/restaurants/{restaurant_id}", response_class=HTMLResponse, response_model=None)
async def restaurant_detail(
    request: Request,
    restaurant_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR))],
) -> HTMLResponse:
    from app.models.restaurant import Restaurant
    r = await db.get(Restaurant, restaurant_id)
    if r is None:
        raise HTTPException(status_code=404, detail="restaurant not found")
    owner = await db.get(User, r.owner_id) if r.owner_id else None
    certs = await cert_service.list_certs_for_restaurant(db, restaurant_id)
    return await _render(
        request, db, "restaurant_detail.html", user, active="restaurants",
        restaurant=r, owner_email=owner.email if owner else "—",
        certs=certs, tiers=list(RestaurantTier),
    )


@router.post("/restaurants/{restaurant_id}/tier", response_model=None)
async def restaurant_set_tier(
    request: Request,
    restaurant_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN))],
    tier: str = Form(...),
    reason: str = Form(...),
) -> Response:
    try:
        new_tier = RestaurantTier(tier)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown tier {tier!r}")
    try:
        await admin_ops.set_restaurant_tier(
            db, restaurant_id=restaurant_id, tier=new_tier, admin=user, reason=reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url=f"/admin/ui/restaurants/{restaurant_id}", status_code=303)


@router.post("/restaurants/{restaurant_id}/certs/{cert_id}/review", response_model=None)
async def cert_review(
    request: Request,
    restaurant_id: uuid.UUID,
    cert_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN))],
    approve: str = Form(...),
    review_notes: Optional[str] = Form(default=None),
) -> Response:
    cert = await cert_service.get_or_404_cert(db, cert_id)
    if cert.status != CertificateStatus.PENDING.value:
        raise HTTPException(status_code=409, detail=f"cert status is {cert.status!r}; only pending can be reviewed")
    await cert_service.review_cert(
        db, cert=cert, approve=(approve.lower() == "true"),
        admin=user, review_notes=review_notes,
    )
    return RedirectResponse(url=f"/admin/ui/restaurants/{restaurant_id}", status_code=303)


# ---------- reviews ----------
@router.get("/reviews", response_class=HTMLResponse, response_model=None)
async def reviews_queue(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR))],
    flagged: int = 0,
) -> HTMLResponse:
    if flagged:
        rows = list((await db.execute(
            select(Review).where(
                Review.moderation_status == ReviewStatus.PENDING.value,
                Review.flagged.is_(True),
            ).order_by(Review.created_at.desc()).limit(200)
        )).scalars().all())
    else:
        rows = list((await db.execute(
            select(Review).where(
                Review.moderation_status == ReviewStatus.PENDING.value,
            ).order_by(Review.created_at.desc()).limit(200)
        )).scalars().all())
    # Hydrate display fields the template needs.
    rest_ids = {r.restaurant_id for r in rows}
    rev_ids = {r.reviewer_id for r in rows}
    from app.models.restaurant import Restaurant
    rmap = {}
    if rest_ids:
        for rr in list((await db.execute(
            select(Restaurant).where(Restaurant.id.in_(rest_ids))
        )).scalars().all()):
            rmap[rr.id] = rr.name
    rusers = {}
    if rev_ids:
        for u in list((await db.execute(
            select(User).where(User.id.in_(rev_ids))
        )).scalars().all()):
            rusers[u.id] = u.display_name
    reviews_view = [
        type("RV", (), {
            "id": r.id, "restaurant_id": r.restaurant_id, "rating": r.rating,
            "body": r.body, "flagged": r.flagged, "flag_reasons": r.flag_reasons,
            "created_at": r.created_at, "reviewer_name": rusers.get(r.reviewer_id, "—"),
        }) for r in rows
    ]
    counts = await admin_ops.queue_counts(db)
    return await _render(
        request, db, "reviews_queue.html", user, active="reviews",
        reviews=reviews_view, restaurant_names=rmap, counts=counts,
        filter_flagged=bool(flagged),
    )


@router.post("/reviews/{review_id}/moderate", response_model=None)
async def review_moderate(
    request: Request,
    review_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR))],
    approve: str = Form(...),
    reason: Optional[str] = Form(default=None),
) -> Response:
    review = await review_service.get_or_404(db, review_id)
    approve_bool = approve.lower() == "true"
    await review_service.moderate(
        db, review=review, admin=user, approve=approve_bool, reason=reason,
    )
    if approve_bool:
        from app.services import points as points_service
        from app.services import referrals as referrals_service
        await points_service.credit_for_review(
            db, reviewer_id=review.reviewer_id, review_id=review.id,
        )
        await referrals_service.credit_referral_if_eligible(
            db, referred_user_id=review.reviewer_id,
        )
    return RedirectResponse(url="/admin/ui/reviews", status_code=303)


# ---------- deals ----------
@router.get("/deals", response_class=HTMLResponse, response_model=None)
async def deals_queue(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR))],
) -> HTMLResponse:
    rows = list((await db.execute(
        select(Deal).where(Deal.status == DealStatus.PENDING_REVIEW.value)
        .order_by(Deal.created_at.desc()).limit(200)
    )).scalars().all())
    from app.models.restaurant import Restaurant
    rmap = {rr.id: rr.name for rr in list((await db.execute(
        select(Restaurant).where(Restaurant.id.in_({d.restaurant_id for d in rows}))
    )).scalars().all())}
    smap = {u.id: u.email for u in list((await db.execute(
        select(User).where(User.id.in_({d.created_by for d in rows}))
    )).scalars().all())}
    return await _render(
        request, db, "deals_queue.html", user, active="deals",
        deals=rows, restaurant_names=rmap, submitter_emails=smap,
    )


@router.get("/deals/new", response_class=HTMLResponse, response_model=None)
async def deal_new_get(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR))],
) -> HTMLResponse:
    from app.models.restaurant import Restaurant
    rsts = list((await db.execute(
        select(Restaurant).order_by(Restaurant.name)
    )).scalars().all())
    return await _render(
        request, db, "deal_new.html", user, active="deals",
        restaurants=rsts, deal_types=list(DealType), error=None,
    )


@router.post("/deals/new", response_model=None)
async def deal_new_post(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR))],
    restaurant_id: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    deal_type: str = Form(...),
    target_audience: str = Form("public"),
    discount_value: str = Form("0"),
    start_date: str = Form(...),
    end_date: str = Form(...),
) -> Response:
    from decimal import Decimal, InvalidOperation
    try:
        rid = uuid.UUID(restaurant_id)
        dt = DealType(deal_type)
        aud = DealAudience(target_audience)
        dval = Decimal(discount_value or "0")
    except (ValueError, InvalidOperation) as exc:
        return await _render(
            request, db, "deal_new.html", user, active="deals",
            restaurants=[], deal_types=list(DealType),
            error=f"Invalid input: {exc}",
        )
    try:
        sd = date.fromisoformat(start_date)
        ed = date.fromisoformat(end_date)
    except ValueError as exc:
        return await _render(
            request, db, "deal_new.html", user, active="deals",
            restaurants=[], deal_types=list(DealType),
            error=f"Invalid date: {exc}",
        )
    from app.models.restaurant import Restaurant
    r = await db.get(Restaurant, rid)
    if r is None:
        raise HTTPException(status_code=404, detail="restaurant not found")
    inp = deal_service.CreateDealInput(
        title=title.strip(),
        deal_type=dt,
        start_date=sd, end_date=ed,
        description=description.strip() or None,
        discount_value=dval,
        target_audience=aud,
    )
    deal = await deal_service.create_hand_curated(
        db, restaurant=r, curator=user, inp=inp,
    )
    return RedirectResponse(url=f"/admin/ui/deals?curated={deal.id}", status_code=303)


@router.post("/deals/{deal_id}/approve", response_model=None)
async def deal_approve(
    request: Request,
    deal_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR))],
) -> Response:
    deal = await deal_service.get_or_404(db, deal_id)
    await deal_service.approve(db, deal=deal, curator=user)
    return RedirectResponse(url="/admin/ui/deals", status_code=303)


@router.post("/deals/{deal_id}/reject", response_model=None)
async def deal_reject(
    request: Request,
    deal_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR))],
    reason: str = Form(...),
) -> Response:
    deal = await deal_service.get_or_404(db, deal_id)
    await deal_service.reject(db, deal=deal, curator=user, reason=reason)
    return RedirectResponse(url="/admin/ui/deals", status_code=303)


# ---------- users ----------
@router.get("/users", response_class=HTMLResponse, response_model=None)
async def users_list(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN))],
    q: str = "",
) -> HTMLResponse:
    stmt = select(User).order_by(User.created_at.desc()).limit(200)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            User.email.ilike(like),
            User.display_name.ilike(like),
        ))
    rows = list((await db.execute(stmt)).scalars().all())
    return await _render(
        request, db, "users_list.html", user, active="users",
        users=rows, q=q,
    )


@router.post("/users/{user_id}/active", response_model=None)
async def user_set_active(
    request: Request,
    user_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_ui_role(UserRole.PLATFORM_ADMIN))],
    is_active: str = Form(...),
) -> Response:
    try:
        await admin_ops.set_user_active(
            db, user_id=user_id,
            is_active=(is_active.lower() == "true"),
            admin=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url="/admin/ui/users", status_code=303)


# ---------- exception handlers ----------
async def _render_error(request: Request, exc: HTTPException) -> HTMLResponse:
    """Friendly error page for 401/403 raised by UI deps. We do not
    template-render via the request's app here because HTTPException
    raised by Depends doesn't expose the response cycle cleanly —
    instead we just inline the simplest page.
    """
    from app.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        user = await get_current_ui_user(request, db)
        queue = await _queue_for_sidebar(db)
    body = render_template(
        "error.html",
        user=user, queue=queue, active="", app_version="0.10.0",
        detail=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
    )
    return HTMLResponse(content=body, status_code=exc.status_code)


# Note: we register these in main.py because routers can't own
# app-level exception handlers cleanly. See app/main.py.
