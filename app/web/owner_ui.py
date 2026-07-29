"""Restaurant owner portal — Stage 11.

Separate router, role-gated to restaurant_owner (and platform_admin
acting on behalf of an owner). Pages:
  /owner/dashboard            overview (restaurant state, pending review, etc.)
  /owner/restaurant/edit      profile fields (name, address, description, hours)
  /owner/photos               list + upload + delete
  /owner/certificates         list + upload halal certificate (pending review)
  /owner/deals                list owner's deals (all states) + submit new
  /owner/tier                 show current tier + Stripe checkout (if needed)

A diner who lands on /owner/* gets 403 with a clear "owner portal"
message. An unauthenticated user is redirected to /web/login.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
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
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.deal import Deal, DealAudience, DealStatus, DealType
from app.models.enums import (
    CertificateStatus,
    RestaurantTier,
    UserRole,
)
from app.models.halal_certificate import HalalCertificate
from app.models.billing import RestaurantBillingSubscription
from app.models.restaurant import Photo, Restaurant
from app.models.user import User
from app.services import billing as billing_service
from app.services import certificates as cert_service
from app.services import deals as deal_service
from app.services import photos as photo_service
from app.services import restaurant_service
from app.web.deps import get_optional_user, require_owner_role
from app.web.templates_env import render

router = APIRouter(prefix="/owner", tags=["web-owner"])


async def _render(request: Request, template: str, user: Optional[User], **ctx) -> HTMLResponse:
    flash = request.session.pop("flash", None) if hasattr(request, "session") else None
    return HTMLResponse(render(template, user=user, flash=flash, app_version="0.11.0",
                              settings=settings, **ctx))


def _set_flash(request: Request, kind: str, text: str) -> None:
    request.session["flash"] = {"kind": kind, "text": text}


async def _resolve_owner_restaurant(
    db: AsyncSession, user: User,
) -> Restaurant:
    """Owner portal only works for users with a restaurant. Admins
    see the same screens but act on the first restaurant they pick
    (URL-driven) — for MVP we just show 'no restaurant' if an admin
    visits without one.
    """
    r = (await db.execute(
        select(Restaurant).where(Restaurant.owner_id == user.id)
        .order_by(Restaurant.created_at.asc()).limit(1)
    )).scalar_one_or_none()
    return r


# ---------- dashboard ----------
@router.get("/dashboard", response_class=HTMLResponse, response_model=None)
async def owner_dashboard(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_owner_role())],
) -> HTMLResponse:
    r = await _resolve_owner_restaurant(db, user)
    if r is None:
        return await _render(
            request, "owner_dashboard.html", user, restaurant=None,
            certs=[], photos=[], deals=[], subscription=None,
            notice="You don't have a restaurant yet. The signup flow will create one on first save — coming soon.",
        )
    certs = list((await db.execute(
        select(HalalCertificate).where(HalalCertificate.restaurant_id == r.id)
        .order_by(HalalCertificate.uploaded_at.desc()).limit(5)
    )).scalars().all())
    photos = list((await db.execute(
        select(Photo).where(Photo.restaurant_id == r.id)
        .order_by(Photo.sort_order, Photo.created_at.desc()).limit(6)
    )).scalars().all())
    deals = list((await db.execute(
        select(Deal).where(Deal.restaurant_id == r.id)
        .order_by(Deal.created_at.desc()).limit(10)
    )).scalars().all())
    subscription = (await db.execute(
        select(RestaurantBillingSubscription).where(
            RestaurantBillingSubscription.restaurant_id == r.id,
        ).order_by(RestaurantBillingSubscription.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    return await _render(
        request, "owner_dashboard.html", user, restaurant=r,
        certs=certs, photos=photos, deals=deals, subscription=subscription,
        notice=None,
    )


# ---------- profile edit ----------
@router.get("/restaurant/edit", response_class=HTMLResponse, response_model=None)
async def owner_edit_get(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_owner_role())],
) -> HTMLResponse:
    r = await _resolve_owner_restaurant(db, user)
    if r is None:
        _set_flash(request, "error", "No restaurant on file yet. Contact support.")
        return RedirectResponse(url="/owner/dashboard", status_code=303)
    return await _render(request, "owner_edit.html", user, restaurant=r, tiers=list(RestaurantTier))


@router.post("/restaurant/edit", response_model=None)
async def owner_edit_post(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_owner_role())],
    name: str = Form(...),
    description: str = Form(default=""),
    address_line: str = Form(...),
    city: str = Form(...),
    state: str = Form(default="TX"),
    postal_code: str = Form(...),
    phone: str = Form(default=""),
    website: str = Form(default=""),
    email: str = Form(default=""),
    price_range: str = Form(default="2"),
) -> Response:
    r = await _resolve_owner_restaurant(db, user)
    if r is None:
        raise HTTPException(status_code=404, detail="no restaurant")
    r.name = name.strip()
    r.description = description.strip() or None
    r.address_line = address_line.strip()
    r.city = city.strip()
    r.state = state.strip()
    r.postal_code = postal_code.strip()
    r.phone = phone.strip() or None
    r.website = website.strip() or None
    r.email = email.strip() or None
    r.price_range = price_range
    await db.commit()
    _set_flash(request, "success", "Restaurant updated.")
    return RedirectResponse(url="/owner/dashboard", status_code=303)


# ---------- photos ----------
@router.get("/photos", response_class=HTMLResponse, response_model=None)
async def owner_photos_get(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_owner_role())],
) -> HTMLResponse:
    r = await _resolve_owner_restaurant(db, user)
    if r is None:
        return RedirectResponse(url="/owner/dashboard", status_code=303)
    photos = list((await db.execute(
        select(Photo).where(Photo.restaurant_id == r.id)
        .order_by(Photo.sort_order, Photo.created_at.desc())
    )).scalars().all())
    return await _render(
        request, "owner_photos.html", user, restaurant=r, photos=photos,
        cap=settings.tier_photo_caps.get(r.tier.value, 2),
    )


@router.post("/photos", response_model=None)
async def owner_photos_post(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_owner_role())],
    caption: str = Form(default=""),
    file: UploadFile = File(...),
) -> Response:
    r = await _resolve_owner_restaurant(db, user)
    if r is None:
        raise HTTPException(status_code=404, detail="no restaurant")
    data = await file.read()
    try:
        await photo_service.upload_photo(
            db, restaurant=r, content=data,
            content_type=file.content_type or "image/jpeg",
            caption=caption.strip() or None,
        )
    except HTTPException as exc:
        _set_flash(request, "error", str(exc.detail))
        return RedirectResponse(url="/owner/photos", status_code=303)
    _set_flash(request, "success", "Photo uploaded.")
    return RedirectResponse(url="/owner/photos", status_code=303)


# ---------- halal certificates ----------
@router.get("/certificates", response_class=HTMLResponse, response_model=None)
async def owner_certs_get(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_owner_role())],
) -> HTMLResponse:
    r = await _resolve_owner_restaurant(db, user)
    if r is None:
        return RedirectResponse(url="/owner/dashboard", status_code=303)
    certs = await cert_service.list_certs_for_restaurant(db, r.id)
    bodies = list((await db.execute(
        select(__import__("app.models.certifying_body", fromlist=["CertifyingBody"]).CertifyingBody)
        .order_by("name")
    )).scalars().all())
    return await _render(
        request, "owner_certs.html", user, restaurant=r, certs=certs, bodies=bodies,
    )


@router.post("/certificates", response_model=None)
async def owner_certs_post(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_owner_role())],
    certifying_body_id: int = Form(...),
    issue_date: str = Form(...),
    expiry_date: str = Form(default=""),
    document: UploadFile = File(...),
) -> Response:
    r = await _resolve_owner_restaurant(db, user)
    if r is None:
        raise HTTPException(status_code=404, detail="no restaurant")
    data = await document.read()
    try:
        issue_d = date.fromisoformat(issue_date)
        exp_d = date.fromisoformat(expiry_date) if expiry_date else None
    except ValueError as exc:
        _set_flash(request, "error", f"Invalid date: {exc}")
        return RedirectResponse(url="/owner/certificates", status_code=303)
    try:
        await cert_service.upload_cert(
            db, restaurant=r, uploaded_by=user, content=data,
            content_type=document.content_type or "application/pdf",
            certifying_body_id=certifying_body_id, issue_date=issue_d, expiry_date=exp_d,
        )
    except HTTPException as exc:
        _set_flash(request, "error", str(exc.detail))
        return RedirectResponse(url="/owner/certificates", status_code=303)
    _set_flash(request, "success", "Certificate submitted. Admin will review within 1-2 business days.")
    return RedirectResponse(url="/owner/certificates", status_code=303)


# ---------- deals ----------
@router.get("/deals", response_class=HTMLResponse, response_model=None)
async def owner_deals_get(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_owner_role())],
) -> HTMLResponse:
    r = await _resolve_owner_restaurant(db, user)
    if r is None:
        return RedirectResponse(url="/owner/dashboard", status_code=303)
    deals = list((await db.execute(
        select(Deal).where(Deal.restaurant_id == r.id)
        .order_by(Deal.created_at.desc())
    )).scalars().all())
    return await _render(
        request, "owner_deals.html", user, restaurant=r, deals=deals,
        deal_types=list(DealType), audiences=list(DealAudience),
    )


@router.post("/deals", response_model=None)
async def owner_deals_post(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_owner_role())],
    title: str = Form(...),
    description: str = Form(default=""),
    deal_type: str = Form(...),
    target_audience: str = Form(default="public"),
    discount_value: str = Form(default="0"),
    start_date: str = Form(...),
    end_date: str = Form(...),
) -> Response:
    r = await _resolve_owner_restaurant(db, user)
    if r is None:
        raise HTTPException(status_code=404, detail="no restaurant")
    try:
        dt = DealType(deal_type)
        aud = DealAudience(target_audience)
        dval = Decimal(discount_value or "0")
        sd = date.fromisoformat(start_date)
        ed = date.fromisoformat(end_date)
    except (ValueError, Exception) as exc:
        _set_flash(request, "error", f"Invalid input: {exc}")
        return RedirectResponse(url="/owner/deals", status_code=303)
    try:
        deal = await deal_service.create_draft(
            db, restaurant=r, owner=user, title=title.strip(),
            description=description.strip() or None,
            deal_type=dt, target_audience=aud,
            discount_value=dval, start_date=sd, end_date=ed,
        )
    except HTTPException as exc:
        _set_flash(request, "error", str(exc.detail))
        return RedirectResponse(url="/owner/deals", status_code=303)
    # Auto-submit for review (BRD: owner drafts and submits to curator queue).
    deal = await deal_service.submit(db, deal=deal, actor=user)
    _set_flash(request, "success", "Deal submitted for review.")
    return RedirectResponse(url="/owner/deals", status_code=303)


@router.post("/deals/{deal_id}/revise", response_model=None)
async def owner_deal_revise(
    request: Request,
    deal_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_owner_role())],
) -> Response:
    r = await _resolve_owner_restaurant(db, user)
    if r is None:
        raise HTTPException(status_code=404, detail="no restaurant")
    deal = await deal_service.get_or_404(db, deal_id)
    if deal.restaurant_id != r.id:
        raise HTTPException(status_code=403, detail="not your deal")
    if deal.status != DealStatus.REJECTED.value:
        _set_flash(request, "error", "Only rejected deals can be revised.")
        return RedirectResponse(url="/owner/deals", status_code=303)
    await deal_service.revise(db, deal=deal, actor=user)
    _set_flash(request, "success", "Deal moved back to draft. Edit and re-submit.")
    return RedirectResponse(url="/owner/deals", status_code=303)


# ---------- tier / subscription ----------
@router.post("/tier/subscribe", response_model=None)
async def owner_tier_subscribe(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_owner_role())],
    target_tier: str = Form(...),
) -> Response:
    r = await _resolve_owner_restaurant(db, user)
    if r is None:
        raise HTTPException(status_code=404, detail="no restaurant")
    try:
        url = await billing_service.create_restaurant_checkout(
            db, restaurant=r, target_tier=target_tier,
            success_url=f"{settings.app_public_url}/owner/dashboard?subscribed=1",
            cancel_url=f"{settings.app_public_url}/owner/dashboard?canceled=1",
        )
    except HTTPException as exc:
        _set_flash(request, "error", f"Could not start checkout: {exc.detail}")
        return RedirectResponse(url="/owner/dashboard", status_code=303)
    return RedirectResponse(url=url, status_code=303)
