"""Billing service — Stripe Checkout + webhook handling (Stage 7).

Architecture:
  - Server never sees card data. We use Stripe Checkout (hosted page) to
    collect payment info; the client gets a URL to redirect to. Our server
    only knows Stripe customer + subscription IDs and the lifecycle events
    Stripe sends us via webhook.
  - Webhook signature is verified via `stripe.Webhook.construct_event`.
    Anything that doesn't verify is rejected (no exceptions).
  - On every event that affects a restaurant or user, we update the
    billing row AND the entity's tier / status atomically. So the photo-cap
    and push-only gate read from `Restaurant.tier` and the user-subscription
    gate reads from `UserBillingSubscription.status` (via
    `get_user_subscription_tier`), and they always agree with the billing row.

Tier rules on Stripe lifecycle events (per BRD §3.4 + the Stage 7 DoD:
"a cancelled/lapsed subscription correctly downgrades access"):
  - active, trialing              → keep current tier
  - past_due                      → KEEP current tier (Stripe retries ~3 weeks;
                                     the DoD wording says "lapsed" = unpaid/canceled,
                                     not past_due, and past_due is the grace window)
  - unpaid, canceled, incomplete_expired → DOWNGRADE to free

Cancel-at-period-end semantics:
  - When `cancel_at_period_end=True` and the period hasn't ended yet,
    the subscription is still "active" in Stripe's eyes and we keep the
    paid tier. When Stripe sends `customer.subscription.deleted` (after
    the period ends) we drop to free.
  - The downgrade happens the moment Stripe fires the deleted event,
    not at the period end we computed ourselves — that keeps us honest
    with whatever Stripe actually does.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import stripe
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.billing import (
    RestaurantBillingSubscription,
    UserBillingSubscription,
)
from app.models.enums import RestaurantTier, UserRole
from app.models.restaurant import Restaurant
from app.models.user import User

logger = logging.getLogger("halalistic.billing")

# Stripe statuses that mean "tier is currently live" (no downgrade).
_KEEP_TIER_STATUSES = {"active", "trialing"}
# Stripe statuses that mean "tier is gone — downgrade now".
_DOWNGRADE_STATUSES = {"unpaid", "canceled", "incomplete_expired"}

# Map our internal tier enum to the configured Stripe price IDs.
_TIER_TO_PRICE_ID = {
    RestaurantTier.PHOTO_PLUS: settings.stripe_price_restaurant_photo_plus,
    RestaurantTier.FEATURED: settings.stripe_price_restaurant_featured,
    RestaurantTier.PREMIUM: settings.stripe_price_restaurant_premium,
}


# ---------- Stripe client init ----------
def _init_stripe() -> None:
    """Idempotent: set the API key on the SDK from settings. Called from
    every entry-point that touches the Stripe API.
    """
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=500,
            detail="STRIPE_SECRET_KEY is not configured; cannot reach Stripe",
        )
    stripe.api_key = settings.stripe_secret_key


# ---------- Price / tier mapping ----------
def price_id_for_tier(tier: RestaurantTier) -> str:
    if tier == RestaurantTier.FREE:
        raise HTTPException(status_code=400, detail="free tier has no Stripe price")
    return _TIER_TO_PRICE_ID[tier]


# ---------- Checkout Session creation ----------
async def create_restaurant_checkout_session(
    db: AsyncSession, *, restaurant: Restaurant, target_tier: RestaurantTier,
    actor: User,
) -> str:
    """Create a Stripe Checkout Session for upgrading a restaurant's tier.

    Returns the URL the client should redirect the user to. The URL is
    short-lived and single-use.
    """
    _init_stripe()
    if actor.role not in (UserRole.RESTAURANT_OWNER, UserRole.PLATFORM_ADMIN):
        raise HTTPException(status_code=403, detail="only owners can start a billing session")
    if restaurant.tier == target_tier:
        raise HTTPException(status_code=409, detail=f"restaurant is already on {target_tier.value}")
    price_id = price_id_for_tier(target_tier)
    success_url = f"{settings.app_public_url}/restaurants/{restaurant.id}?billing=ok"
    cancel_url = f"{settings.app_public_url}/restaurants/{restaurant.id}?billing=cancel"

    # Reuse a Stripe customer if we've seen this restaurant before, else
    # let Checkout create one and we'll capture it from the webhook.
    existing = await db.scalar(
        select(RestaurantBillingSubscription).where(
            RestaurantBillingSubscription.restaurant_id == restaurant.id
        )
    )
    customer_id = existing.stripe_customer_id if existing else None

    session_params: dict = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {
            "kind": "restaurant",
            "restaurant_id": str(restaurant.id),
            "target_tier": target_tier.value,
        },
        "subscription_data": {
            "metadata": {
                "kind": "restaurant",
                "restaurant_id": str(restaurant.id),
                "target_tier": target_tier.value,
            },
        },
    }
    if customer_id:
        session_params["customer"] = customer_id
    else:
        session_params["customer_email"] = actor.email

    session = stripe.checkout.Session.create(**session_params)
    return session.url


async def create_user_checkout_session(
    db: AsyncSession, *, user: User,
) -> str:
    """Create a Stripe Checkout Session for the user's deals subscription.

    Returns the URL the client should redirect to. Single flat-rate tier
    per BRD §3.6 (no differentiated pricing on the user side).
    """
    _init_stripe()
    success_url = f"{settings.app_public_url}/?billing=ok"
    cancel_url = f"{settings.app_public_url}/?billing=cancel"
    existing = await db.scalar(
        select(UserBillingSubscription).where(UserBillingSubscription.user_id == user.id)
    )
    customer_id = existing.stripe_customer_id if existing else None

    session_params: dict = {
        "mode": "subscription",
        "line_items": [{"price": settings.stripe_price_user_deals, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {"kind": "user", "user_id": str(user.id)},
        "subscription_data": {
            "metadata": {"kind": "user", "user_id": str(user.id)},
        },
    }
    if customer_id:
        session_params["customer"] = customer_id
    else:
        session_params["customer_email"] = user.email

    session = stripe.checkout.Session.create(**session_params)
    return session.url


# ---------- Webhook signature verification ----------
class WebhookVerificationFailed(Exception):
    """Raised when a webhook payload fails signature verification. The
    endpoint should map this to a 400. We log it so attempts are visible
    in production but never echo the failure detail to the caller
    (don't help attackers debug their payload forgery).
    """


def verify_webhook_payload(payload: bytes, signature_header: str) -> dict:
    """Verify the signature on a Stripe webhook payload. Returns the
    parsed event dict on success. Raises WebhookVerificationFailed on
    ANY failure (bad signature, missing header, wrong secret).
    """
    if not settings.stripe_webhook_secret:
        logger.error("STRIPE_WEBHOOK_SECRET is not configured; refusing all webhooks")
        raise WebhookVerificationFailed("webhook secret not configured")
    if not signature_header:
        raise WebhookVerificationFailed("missing signature header")
    try:
        event = stripe.Webhook.construct_event(
            payload, signature_header, settings.stripe_webhook_secret,
        )
    except stripe.error.SignatureVerificationError as exc:
        raise WebhookVerificationFailed(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — stripe may raise other things
        raise WebhookVerificationFailed(str(exc)) from exc
    return event.to_dict_recursive() if hasattr(event, "to_dict_recursive") else dict(event)


# ---------- Webhook event handlers ----------
async def handle_event(db: AsyncSession, event: dict) -> None:
    """Dispatch by event type. Each handler is idempotent (Stripe may
    deliver the same event more than once per their docs).
    """
    etype = event.get("type")
    data = event.get("data", {}).get("object", {})
    if etype == "checkout.session.completed":
        await _on_checkout_completed(db, event, data)
    elif etype == "customer.subscription.created":
        await _on_subscription_change(db, data)
    elif etype == "customer.subscription.updated":
        await _on_subscription_change(db, data)
    elif etype == "customer.subscription.deleted":
        await _on_subscription_change(db, data)
    elif etype == "invoice.payment_failed":
        await _on_invoice_payment_failed(db, data)
    elif etype == "invoice.paid":
        await _on_invoice_paid(db, data)
    else:
        logger.info("ignoring Stripe event type: %s", etype)


async def _on_checkout_completed(db: AsyncSession, event: dict, session: dict) -> None:
    """The Checkout Session has been paid. Stripe will fire
    customer.subscription.created shortly, which is what creates our
    subscription row. We pre-create the row here so we have a place to
    put the customer_id; the actual tier will be set by the subscription
    event.
    """
    md = session.get("metadata") or {}
    kind = md.get("kind")
    customer_id = session.get("customer")
    sub_id = session.get("subscription")
    if kind == "restaurant":
        restaurant_id = md.get("restaurant_id")
        if not restaurant_id:
            return
        from uuid import UUID
        try:
            rid = UUID(restaurant_id)
        except ValueError:
            return
        existing = await db.scalar(
            select(RestaurantBillingSubscription).where(
                RestaurantBillingSubscription.restaurant_id == rid
            )
        )
        if existing is None:
            db.add(RestaurantBillingSubscription(
                id=__import__("uuid").uuid4(),
                restaurant_id=rid,
                stripe_customer_id=customer_id,
                stripe_subscription_id=sub_id,
                tier="free",  # real tier set by the subscription.* event
                status="incomplete",
            ))
        else:
            existing.stripe_customer_id = customer_id or existing.stripe_customer_id
            existing.stripe_subscription_id = sub_id or existing.stripe_subscription_id
        await db.commit()
    elif kind == "user":
        user_id = md.get("user_id")
        if not user_id:
            return
        from uuid import UUID
        try:
            uid = UUID(user_id)
        except ValueError:
            return
        existing = await db.scalar(
            select(UserBillingSubscription).where(UserBillingSubscription.user_id == uid)
        )
        if existing is None:
            db.add(UserBillingSubscription(
                id=__import__("uuid").uuid4(),
                user_id=uid,
                stripe_customer_id=customer_id,
                stripe_subscription_id=sub_id,
                status="incomplete",
            ))
        else:
            existing.stripe_customer_id = customer_id or existing.stripe_customer_id
            existing.stripe_subscription_id = sub_id or existing.stripe_subscription_id
        await db.commit()


async def _on_subscription_change(db: AsyncSession, sub: dict) -> None:
    """customer.subscription.{created,updated,deleted} all funnel here.

    The big rule: keep tier in `_KEEP_TIER_STATUSES`, downgrade to free
    in `_DOWNGRADE_STATUSES`, leave as-is if status is anything else.
    Also update `cancel_at_period_end` and `current_period_end` for the
    billing row. The tier update is atomic with the restaurant row.
    """
    md = sub.get("metadata") or {}
    kind = md.get("kind")
    stripe_status = sub.get("status")
    sub_id = sub.get("id")
    customer_id = sub.get("customer")
    cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    current_period_end_ts = sub.get("current_period_end")
    current_period_end = (
        datetime.fromtimestamp(current_period_end_ts, tz=timezone.utc)
        if current_period_end_ts else None
    )

    if kind == "restaurant":
        restaurant_id = md.get("restaurant_id")
        target_tier_str = md.get("target_tier")  # set at checkout time
        if not restaurant_id:
            return
        from uuid import UUID
        try:
            rid = UUID(restaurant_id)
        except ValueError:
            return

        row = await db.scalar(
            select(RestaurantBillingSubscription).where(
                RestaurantBillingSubscription.restaurant_id == rid
            )
        )
        if row is None:
            row = RestaurantBillingSubscription(
                id=__import__("uuid").uuid4(),
                restaurant_id=rid,
                stripe_customer_id=customer_id,
                stripe_subscription_id=sub_id,
                tier="free",
                status=stripe_status or "incomplete",
            )
            db.add(row)
        else:
            row.stripe_customer_id = customer_id or row.stripe_customer_id
            row.stripe_subscription_id = sub_id or row.stripe_subscription_id
            row.status = stripe_status or row.status
        row.cancel_at_period_end = cancel_at_period_end
        row.current_period_end = current_period_end

        # Apply tier rules. `new_tier` is None for transient statuses
        # (past_due, incomplete, etc.) — in that case we keep whatever
        # the row currently has and do NOT touch the restaurant tier.
        new_tier = _resolve_restaurant_tier(stripe_status, target_tier_str)
        if new_tier is not None:
            row.tier = new_tier.value
            restaurant = await db.get(Restaurant, rid)
            if restaurant is not None and restaurant.tier != new_tier:
                restaurant.tier = new_tier
        await db.commit()
    elif kind == "user":
        user_id = md.get("user_id")
        if not user_id:
            return
        from uuid import UUID
        try:
            uid = UUID(user_id)
        except ValueError:
            return
        row = await db.scalar(
            select(UserBillingSubscription).where(
                UserBillingSubscription.user_id == uid
            )
        )
        if row is None:
            row = UserBillingSubscription(
                id=__import__("uuid").uuid4(),
                user_id=uid,
                stripe_customer_id=customer_id,
                stripe_subscription_id=sub_id,
                status=stripe_status or "incomplete",
            )
            db.add(row)
        else:
            row.stripe_customer_id = customer_id or row.stripe_customer_id
            row.stripe_subscription_id = sub_id or row.stripe_subscription_id
            row.status = stripe_status or row.status
        row.cancel_at_period_end = cancel_at_period_end
        row.current_period_end = current_period_end
        await db.commit()


async def _on_invoice_payment_failed(db: AsyncSession, invoice: dict) -> None:
    """Mark the subscription as past_due. We keep the tier (Stripe
    retries for ~3 weeks). If retries exhaust, Stripe sends a
    subscription.updated event with status=unpaid which our normal
    handler will catch and downgrade.

    Stage 9: also send a "payment failed" email to the user / restaurant
    owner so they can fix the card before the retry window closes.
    """
    from app.services.email import send_billing_payment_failed
    sub_id = invoice.get("subscription")
    if not sub_id:
        return
    amount_due = invoice.get("amount_due") or 0
    # Find both possible rows and update.
    for model in (RestaurantBillingSubscription, UserBillingSubscription):
        row = await db.scalar(
            select(model).where(model.stripe_subscription_id == sub_id)
        )
        if row is not None:
            row.status = "past_due"
            await db.commit()
            # Best-effort: email the owner / user. We resolve the
            # recipient from the FK (restaurant_id for restaurants,
            # user_id for users) and look up the email.
            from app.models.restaurant import Restaurant
            from app.models.user import User
            if isinstance(row, RestaurantBillingSubscription):
                rest = await db.get(Restaurant, row.restaurant_id)
                if rest is not None:
                    owner = await db.get(User, rest.owner_id) if rest.owner_id else None
                    if owner is not None:
                        send_billing_payment_failed(owner.email, int(amount_due))
            else:
                user = await db.get(User, row.user_id)
                if user is not None:
                    send_billing_payment_failed(user.email, int(amount_due))
            return


async def _on_invoice_paid(db: AsyncSession, invoice: dict) -> None:
    """Stage 9: send a receipt email on successful payment. Idempotent:
    Stripe may deliver invoice.paid multiple times, but the email is
    best-effort and the user will just get a duplicate (acceptable for
    a receipt; Stripe's own customer-facing UI also handles this).
    """
    from app.services.email import send_billing_receipt
    sub_id = invoice.get("subscription")
    if not sub_id:
        return
    amount_paid = invoice.get("amount_paid") or invoice.get("amount_due") or 0
    description = (invoice.get("lines", {}).get("data") or [{}])[0].get("description") or "Halalistic subscription"
    description = (description or "")[:200]
    for model in (RestaurantBillingSubscription, UserBillingSubscription):
        row = await db.scalar(
            select(model).where(model.stripe_subscription_id == sub_id)
        )
        if row is not None:
            from app.models.restaurant import Restaurant
            from app.models.user import User
            if isinstance(row, RestaurantBillingSubscription):
                rest = await db.get(Restaurant, row.restaurant_id)
                if rest is not None:
                    owner = await db.get(User, rest.owner_id) if rest.owner_id else None
                    if owner is not None:
                        send_billing_receipt(owner.email, int(amount_paid),
                                              f"{rest.name} — {row.tier} tier")
            else:
                user = await db.get(User, row.user_id)
                if user is not None:
                    send_billing_receipt(user.email, int(amount_paid), "Halalistic deals subscription")
            return


def _resolve_restaurant_tier(
    stripe_status: Optional[str], target_tier_str: Optional[str],
) -> RestaurantTier:
    """Apply the tier rules. If status is in `_KEEP_TIER_STATUSES`, use
    the target_tier from the metadata. If in `_DOWNGRADE_STATUSES`,
    force `free`. If status is something else (e.g. incomplete), keep
    whatever we have.
    """
    if stripe_status in _DOWNGRADE_STATUSES:
        return RestaurantTier.FREE
    if stripe_status in _KEEP_TIER_STATUSES and target_tier_str:
        try:
            return RestaurantTier(target_tier_str)
        except ValueError:
            return RestaurantTier.FREE
    # Unknown / transient status — don't change the tier.
    return None  # caller treats None as "leave the row as-is"


# ---------- Read helpers (for the API) ----------
async def get_restaurant_billing(
    db: AsyncSession, restaurant_id,
) -> Optional[RestaurantBillingSubscription]:
    return await db.scalar(
        select(RestaurantBillingSubscription).where(
            RestaurantBillingSubscription.restaurant_id == restaurant_id
        )
    )


async def get_user_billing(
    db: AsyncSession, user_id,
) -> Optional[UserBillingSubscription]:
    return await db.scalar(
        select(UserBillingSubscription).where(
            UserBillingSubscription.user_id == user_id
        )
    )


# ---------- Public gate used by Stage 6 (push-only, premium-only) ----------
async def get_user_subscription_tier(db: AsyncSession, user_id) -> str:
    """Real implementation (replaces the Stage 6 stub). Returns
    'premium' if the user has an active Stripe subscription, else 'free'.
    """
    row = await db.scalar(
        select(UserBillingSubscription).where(UserBillingSubscription.user_id == user_id)
    )
    if row is None:
        return "free"
    if row.status in _KEEP_TIER_STATUSES and not row.cancel_at_period_end:
        return "premium"
    return "free"
