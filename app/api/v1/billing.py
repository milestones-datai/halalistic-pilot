"""Billing API — Stripe Checkout + webhook receiver (Stage 7).

Endpoints
---------
Owner (RESTAURANT_OWNER or PLATFORM_ADMIN):
  POST /api/v1/restaurants/{rid}/billing/checkout-session
        body: {"target_tier": "photo_plus" | "featured" | "premium"}
        → {"checkout_url": "https://checkout.stripe.com/..."}
  GET  /api/v1/restaurants/{rid}/billing
        → current billing row (status, tier, current_period_end, ...)

User (Diner or any):
  POST /api/v1/users/me/billing/checkout-session
        → {"checkout_url": "https://checkout.stripe.com/..."}
  GET  /api/v1/users/me/billing
        → current billing row

Webhook (no auth, signature-verified):
  POST /api/v1/webhooks/stripe
        raw body + Stripe-Signature header
        → 200 OK on success, 400 on signature failure
"""
from __future__ import annotations

import logging
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps.auth import get_current_user, require_role
from app.models.billing import RestaurantBillingSubscription, UserBillingSubscription
from app.models.enums import RestaurantTier, UserRole
from app.models.user import User
from app.services import billing as billing_service
from app.services.restaurant_service import get_or_404 as get_restaurant_or_404

logger = logging.getLogger("halalistic.billing")

router = APIRouter(prefix="/billing", tags=["billing"])
restaurant_billing_router = APIRouter(prefix="/restaurants", tags=["billing"])
user_billing_router = APIRouter(prefix="/users", tags=["billing"])
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ---- Schemas ----
class CheckoutIn(BaseModel):
    target_tier: RestaurantTier


class CheckoutOut(BaseModel):
    checkout_url: str


class RestaurantBillingOut(BaseModel):
    tier: str
    status: str
    cancel_at_period_end: bool
    current_period_end: Optional[str] = None
    stripe_subscription_id: Optional[str] = None


class UserBillingOut(BaseModel):
    status: str
    cancel_at_period_end: bool
    current_period_end: Optional[str] = None
    stripe_subscription_id: Optional[str] = None


# ---- Owner: restaurant tier upgrade ----
@restaurant_billing_router.post(
    "/{restaurant_id}/billing/checkout-session",
    response_model=CheckoutOut,
)
async def create_restaurant_checkout(
    restaurant_id: uuid.UUID,
    body: CheckoutIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_role(UserRole.RESTAURANT_OWNER, UserRole.PLATFORM_ADMIN))],
) -> CheckoutOut:
    restaurant = await get_restaurant_or_404(db, restaurant_id)
    is_admin = actor.role == UserRole.PLATFORM_ADMIN
    from app.services.restaurant_service import _ensure_owner_or_admin
    _ensure_owner_or_admin(restaurant, actor, is_admin=is_admin)
    url = await billing_service.create_restaurant_checkout_session(
        db, restaurant=restaurant, target_tier=body.target_tier, actor=actor,
    )
    return CheckoutOut(checkout_url=url)


@restaurant_billing_router.get(
    "/{restaurant_id}/billing",
    response_model=RestaurantBillingOut,
)
async def get_restaurant_billing(
    restaurant_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> RestaurantBillingOut:
    restaurant = await get_restaurant_or_404(db, restaurant_id)
    is_admin = actor.role == UserRole.PLATFORM_ADMIN
    from app.services.restaurant_service import _ensure_owner_or_admin
    _ensure_owner_or_admin(restaurant, actor, is_admin=is_admin)
    row = await billing_service.get_restaurant_billing(db, restaurant_id)
    if row is None:
        return RestaurantBillingOut(
            tier=restaurant.tier.value, status="none",
            cancel_at_period_end=False, current_period_end=None, stripe_subscription_id=None,
        )
    return RestaurantBillingOut(
        tier=row.tier,
        status=row.status,
        cancel_at_period_end=row.cancel_at_period_end,
        current_period_end=row.current_period_end.isoformat() if row.current_period_end else None,
        stripe_subscription_id=row.stripe_subscription_id,
    )


# ---- User: deals subscription ----
@user_billing_router.post(
    "/me/billing/checkout-session",
    response_model=CheckoutOut,
)
async def create_user_checkout(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> CheckoutOut:
    url = await billing_service.create_user_checkout_session(db, user=actor)
    return CheckoutOut(checkout_url=url)


@user_billing_router.get(
    "/me/billing",
    response_model=UserBillingOut,
)
async def get_user_billing(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(get_current_user)],
) -> UserBillingOut:
    row = await billing_service.get_user_billing(db, actor.id)
    if row is None:
        return UserBillingOut(
            status="none", cancel_at_period_end=False,
            current_period_end=None, stripe_subscription_id=None,
        )
    return UserBillingOut(
        status=row.status,
        cancel_at_period_end=row.cancel_at_period_end,
        current_period_end=row.current_period_end.isoformat() if row.current_period_end else None,
        stripe_subscription_id=row.stripe_subscription_id,
    )


# ---- Webhook (signature-verified) ----
@webhook_router.post("/stripe", include_in_schema=False)
async def stripe_webhook(
    request: Request,
    stripe_signature: Annotated[Optional[str], Header(alias="stripe-signature")] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,  # type: ignore[assignment]
) -> dict:
    """Stripe webhook receiver. No bearer auth — Stripe authenticates
    via the `Stripe-Signature` header instead. We verify the signature
    via `stripe.Webhook.construct_event` and reject anything unsigned
    or invalid. We do NOT log the raw payload (it could include
    sensitive data per BRD §7).
    """
    payload = await request.body()
    try:
        event = billing_service.verify_webhook_payload(payload, stripe_signature or "")
    except billing_service.WebhookVerificationFailed as exc:
        # Log enough to investigate, not enough to help an attacker.
        logger.warning("webhook signature verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="invalid signature")
    try:
        await billing_service.handle_event(db, event)
    except Exception as exc:  # noqa: BLE001
        # Surface a 500 to Stripe so they retry — but log the event_id
        # for traceability.
        event_id = event.get("id", "?")
        event_type = event.get("type", "?")
        logger.exception("webhook handler failed for event %s (%s)", event_id, event_type)
        raise HTTPException(status_code=500, detail="handler failure")
    return {"received": True}
