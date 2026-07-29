"""Consumer-facing web app — Stage 11.

Server-rendered Jinja2 + HTMX, using the same signed-cookie session as
the admin console. Routes:

  /                         consumer home (search shortcut + featured)
  /web/signup, /web/login,  auth
  /web/logout
  /web/password-reset

  /restaurants              list + search
  /restaurants/{id}         profile (menu, reviews, deals, cert, photos)
  /restaurants/{id}/review  submit review (Diner only)
  /deals                   list + filter by subscription tier
  /account                  diner dashboard (points, referrals, gift card, sub)
  /account/redeem           gift card request
  /account/subscribe        Stripe Checkout redirect
  /share/{deal_id}          (already in Stage 9 /api/v1/share/*)

The owner portal lives in `app/web/owner_ui.py` (separate router, owner
role only).
"""
from __future__ import annotations

import secrets
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
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.core.config import settings
from app.db.session import get_db
from app.deps.auth import get_current_user
from app.models.deal import Deal, DealAudience, DealStatus, DealType
from app.models.enums import (
    RestaurantTier,
    ReviewStatus,
    UserRole,
)
from app.models.billing import (
    RestaurantBillingSubscription,
    UserBillingSubscription,
)
from app.models.review import Review
from app.models.user import User
from app.services import auth_service, deals as deal_service
from app.services import reviews as review_service
from app.services import points as points_service
from app.services import gift_cards as gift_card_service
from app.services import billing as billing_service
from app.services import certificates as halal_service
from app.services import tags as tag_service
from app.web.deps import get_optional_user, require_consumer_role
from app.web.templates_env import render

router = APIRouter(tags=["web"])


# ---------- helpers ----------
async def _render(request: Request, template: str, user: Optional[User], **ctx) -> HTMLResponse:
    flash = request.session.pop("flash", None) if hasattr(request, "session") else None
    return HTMLResponse(render(template, user=user, flash=flash, app_version="0.11.0",
                              settings=settings, **ctx))


def _set_flash(request: Request, kind: str, text: str) -> None:
    request.session["flash"] = {"kind": kind, "text": text}


def _home_redirect_for(user: User) -> str:
    """Where to land a freshly-logged-in user, by role."""
    if user.role == UserRole.RESTAURANT_OWNER:
        return "/owner/dashboard"
    if user.role in (UserRole.PLATFORM_ADMIN, UserRole.DEAL_CURATOR):
        return "/admin/ui/dashboard"
    return "/"


# ---------- home ----------
@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    user = await get_optional_user(request, db)
    # Featured: top halal-verified restaurants with at least one approved review.
    from app.models.restaurant import Restaurant
    featured = list((await db.execute(
        select(Restaurant).where(
            Restaurant.halal_status == "verified",
            Restaurant.is_active.is_(True),
        ).order_by(Restaurant.created_at.desc()).limit(6)
    )).scalars().all())
    # Active deals (limited slice)
    today = date.today()
    deals = list((await db.execute(
        select(Deal).where(
            Deal.status == DealStatus.APPROVED.value,
            Deal.start_date <= today,
            Deal.end_date >= today,
            Deal.target_audience == DealAudience.PUBLIC.value,
        ).order_by(Deal.created_at.desc()).limit(6)
    )).scalars().all())
    return await _render(request, "home.html", user, featured=featured, deals=deals)


# ---------- auth ----------
@router.get("/web/signup", response_class=HTMLResponse, response_model=None)
async def signup_get(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    user = await get_optional_user(request, db)
    if user is not None:
        return RedirectResponse(url=_home_redirect_for(user), status_code=303)
    return await _render(request, "auth_signup.html", user, error=None, ref_code=request.query_params.get("ref") or "")


@router.post("/web/signup", response_model=None)
async def signup_post(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(...),
    role: str = Form(...),
    referral_code: str = Form(default=""),
) -> Response:
    if role not in (UserRole.DINER.value, UserRole.RESTAURANT_OWNER.value):
        return await _render(request, "auth_signup.html", None, error="Please pick a valid role.")
    try:
        new_user = await auth_service.register_user(
            db,
            auth_service.RegisterInput(
                email=email.strip().lower(),
                password=password,
                display_name=display_name.strip(),
                role=role,
            ),
        )
    except HTTPException as exc:
        return await _render(request, "auth_signup.html", None, error=exc.detail)
    # If a referral code was supplied, link the new user to the referrer
    # (no points credit here — A/C triggers fire later, per Stage 8).
    if referral_code.strip():
        from app.services import referrals as referrals_service
        await referrals_service.attach_referrer_on_register(
            db, new_user=new_user, ref_code=referral_code.strip(),
        )
    request.session["user_id"] = str(new_user.id)
    return RedirectResponse(url=_home_redirect_for(new_user), status_code=303)


@router.get("/web/login", response_class=HTMLResponse, response_model=None)
async def login_get(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    user = await get_optional_user(request, db)
    if user is not None:
        return RedirectResponse(url=_home_redirect_for(user), status_code=303)
    return await _render(request, "auth_login.html", user, error=None)


@router.post("/web/login", response_model=None)
async def login_post(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    email: str = Form(...),
    password: str = Form(...),
) -> Response:
    try:
        user, _ = await auth_service.login(db, email.strip().lower(), password)
    except HTTPException as exc:
        return await _render(request, "auth_login.html", None, error=exc.detail)
    request.session["user_id"] = str(user.id)
    nxt = request.query_params.get("next")
    if not nxt or not nxt.startswith("/"):
        nxt = _home_redirect_for(user)
    return RedirectResponse(url=nxt, status_code=303)


@router.post("/web/logout", response_model=None)
async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


@router.get("/web/password-reset", response_class=HTMLResponse, response_model=None)
async def password_reset_get(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    user = await get_optional_user(request, db)
    return await _render(request, "auth_password_reset.html", user, message=None)


@router.post("/web/password-reset", response_model=None)
async def password_reset_post(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    email: str = Form(...),
) -> Response:
    await auth_service.request_password_reset(db, email.strip().lower())
    return await _render(
        request, "auth_password_reset.html", None,
        message="If that email is on file, a reset link is on its way.",
    )
