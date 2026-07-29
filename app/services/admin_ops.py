"""Admin operations service — Stage 10.

Operations exposed via the internal admin/curator console that don't
have a corresponding public API endpoint yet:

  - `set_user_active(db, user_id, is_active)` — soft-deactivate abusive
    accounts. Reversible.
  - `set_restaurant_tier(db, restaurant_id, tier, admin, reason)` — override
    a restaurant's tier outside the Stripe self-service flow (e.g. comps
    for launch partners, corrections after a billing dispute).
  - `dashboard_kpis(db)` — counts for the pilot KPIs from BRD §9.3:
    restaurants onboarded, active users, subscribed users.

All write operations are audit-friendly: they accept the acting admin
and write through the same `get_db` session the request lives in, so
the unit-of-work is consistent with whatever else the route does.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import RestaurantTier, UserRole
from app.models.restaurant import Restaurant
from app.models.user import User

logger = logging.getLogger("halalistic.admin_ops")


# ---------- user deactivation ----------
async def set_user_active(
    db: AsyncSession, *, user_id: UUID, is_active: bool, admin: User,
) -> User:
    """Soft-deactivate (or reactivate) a user. Reversible. The user's
    JWT will continue to validate until it expires — we additionally
    revoke all active refresh tokens on deactivation so they can't
    rotate into a fresh access token.
    """
    if admin.id == user_id and not is_active:
        raise ValueError("cannot deactivate yourself")
    user = await db.get(User, user_id)
    if user is None:
        raise ValueError("user not found")
    if user.role == UserRole.PLATFORM_ADMIN and not is_active:
        # Defense in depth: don't allow an admin to demote the last
        # platform_admin. We don't strictly enforce "last" here — the
        # founder can still recover via DB if needed — but we log loud.
        from sqlalchemy import func as _f
        n = (await db.execute(
            select(_f.count()).select_from(User).where(
                User.role == UserRole.PLATFORM_ADMIN.value,
                User.is_active.is_(True),
            )
        )).scalar_one()
        if n <= 1:
            raise ValueError("cannot deactivate the last active platform_admin")
    user.is_active = is_active
    if not is_active:
        from app.models.user import RefreshToken
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
    await db.commit()
    await db.refresh(user)
    logger.info("admin %s %s user %s (active=%s)",
                admin.id, "deactivated" if not is_active else "reactivated",
                user.id, is_active)
    return user


# ---------- restaurant tier override ----------
async def set_restaurant_tier(
    db: AsyncSession, *, restaurant_id: UUID, tier: RestaurantTier,
    admin: User, reason: str,
) -> Restaurant:
    """Override a restaurant's tier. This bypasses Stripe — intended for
    comps, corrections, and pilot-launch concessions. The `reason` is
    required and logged for audit.
    """
    if not reason or not reason.strip():
        raise ValueError("reason is required for tier override")
    r = await db.get(Restaurant, restaurant_id)
    if r is None:
        raise ValueError("restaurant not found")
    old = r.tier
    r.tier = tier
    await db.commit()
    await db.refresh(r)
    logger.info("admin %s changed restaurant %s tier: %s -> %s (reason: %s)",
                admin.id, r.id, old, tier, reason.strip())
    return r


# ---------- dashboard KPIs (BRD §9.3) ----------
async def dashboard_kpis(db: AsyncSession) -> dict:
    """Return the pilot KPIs. Kept simple per the brief — not a full
    analytics platform. All numbers are point-in-time counts.
    """
    n_restaurants_total = (await db.execute(
        select(func.count()).select_from(Restaurant)
    )).scalar_one()
    n_restaurants_active = (await db.execute(
        select(func.count()).select_from(Restaurant).where(Restaurant.is_active.is_(True))
    )).scalar_one()
    n_restaurants_verified = (await db.execute(
        select(func.count()).select_from(Restaurant).where(
            Restaurant.halal_status == "verified",
        )
    )).scalar_one()

    n_users_total = (await db.execute(
        select(func.count()).select_from(User)
    )).scalar_one()
    n_users_active = (await db.execute(
        select(func.count()).select_from(User).where(User.is_active.is_(True))
    )).scalar_one()
    # "subscribed" = has an active or trialing billing subscription.
    # We import lazily to avoid a circular import.
    from app.models.billing import RestaurantBillingSubscription, UserBillingSubscription
    n_restaurants_subscribed = (await db.execute(
        select(func.count()).select_from(RestaurantBillingSubscription).where(
            RestaurantBillingSubscription.status.in_(("active", "trialing")),
        )
    )).scalar_one()
    n_users_subscribed = (await db.execute(
        select(func.count()).select_from(UserBillingSubscription).where(
            UserBillingSubscription.status.in_(("active", "trialing")),
        )
    )).scalar_one()

    return {
        "restaurants": {
            "total": n_restaurants_total,
            "active": n_restaurants_active,
            "halal_verified": n_restaurants_verified,
            "subscribed": n_restaurants_subscribed,
        },
        "users": {
            "total": n_users_total,
            "active": n_users_active,
            "subscribed": n_users_subscribed,
        },
    }


# ---------- queue counts (for the sidebar badges) ----------
async def queue_counts(db: AsyncSession) -> dict:
    """Pending counts shown in the admin sidebar so curators know what's
    waiting. Cheaper than re-running the full pending queries.
    """
    from app.models.deal import Deal, DealStatus
    from app.models.enums import ReviewStatus
    from app.models.halal_certificate import HalalCertificate
    from app.models.review import Review
    n_certs = (await db.execute(
        select(func.count()).select_from(HalalCertificate).where(
            HalalCertificate.status == "pending",
        )
    )).scalar_one()
    n_reviews = (await db.execute(
        select(func.count()).select_from(Review).where(
            Review.moderation_status == ReviewStatus.PENDING.value,
        )
    )).scalar_one()
    n_reviews_flagged = (await db.execute(
        select(func.count()).select_from(Review).where(
            Review.moderation_status == ReviewStatus.PENDING.value,
            Review.flagged.is_(True),
        )
    )).scalar_one()
    n_deals = (await db.execute(
        select(func.count()).select_from(Deal).where(
            Deal.status == DealStatus.PENDING_REVIEW.value,
        )
    )).scalar_one()
    return {
        "pending_certs": n_certs,
        "pending_reviews": n_reviews,
        "flagged_reviews": n_reviews_flagged,
        "pending_deals": n_deals,
    }
