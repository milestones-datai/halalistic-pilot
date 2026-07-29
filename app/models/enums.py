"""Enumerated types used across the domain.

Stage 2: UserRole.
Stage 3: RestaurantTier, PriceRange, HalalStatus, HalalVerificationSource.
"""
from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    DINER = "diner"
    RESTAURANT_OWNER = "restaurant_owner"
    DEAL_CURATOR = "deal_curator"
    PLATFORM_ADMIN = "platform_admin"

    @classmethod
    def signup_allowed(cls) -> list[str]:
        return [cls.DINER.value, cls.RESTAURANT_OWNER.value]

    @classmethod
    def admin_assignable(cls) -> list[str]:
        return [cls.DEAL_CURATOR.value, cls.PLATFORM_ADMIN.value]


class RestaurantTier(StrEnum):
    """Per BRD §3.4: 4 paid-feature tiers. Halal verification is a SEPARATE
    concept that lives on `restaurant.halal_status` (HalalStatus enum) and
    is independent of paid tier — that decoupling is the whole point of
    the 4-tier model.

    - free:       standard listing, basic photo cap, no boost
    - photo_plus: more photos per restaurant
    - featured:   boosted search placement
    - premium:    push-notification access (push-only deals) + biggest photo cap
    """
    FREE = "free"
    PHOTO_PLUS = "photo_plus"
    FEATURED = "featured"
    PREMIUM = "premium"


class PriceRange(StrEnum):
    """$-$$$$ scale, stored as strings for forward-compat with localization."""
    BUDGET = "1"        # $
    MODERATE = "2"       # $$
    UPSCALE = "3"       # $$$
    LUXURY = "4"         # $$$$


class HalalStatus(StrEnum):
    """Per BRD §3.2 — verification_status (independent of source)."""
    UNVERIFIED = "unverified"
    PENDING = "pending"
    VERIFIED = "verified"
    REVOKED = "revoked"


class HalalVerificationSource(StrEnum):
    """Per BRD §3.2 — verification_source enum."""
    CERTIFIED = "certified"
    SELF_REPORTED = "self_reported"
    CROWD_VERIFIED = "crowd_verified"


class CertificateStatus(StrEnum):
    """Lifecycle of a HalalCertificate per BRD §3.2.
    PENDING → admin reviews → APPROVED (sets restaurant.halal_status=verified, source=certified)
                         → REJECTED (admin supplies notes, restaurant stays unverified/self_reported)
    EXPIRED → only set by a future scheduled job (Stage 4 leaves this as TODO).
    """
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ReviewStatus(StrEnum):
    """Lifecycle of a Review per BRD §3.3.

    Pre-moderation: every new review starts PENDING and stays that way until
    an admin explicitly approves or rejects it. Unflagged reviews do NOT
    auto-publish.
    """
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class DealStatus(StrEnum):
    """Lifecycle of a Deal per BRD §3.5.

    Full state machine (server-side transition validation in
    app/services/deals.py):

        (none)        --create owner-->     DRAFT
        (none)        --create curator-->   APPROVED   (hand-curated premium)
        DRAFT         --owner submit-->     PENDING_REVIEW
        PENDING_REVIEW --curator approve--> APPROVED
        PENDING_REVIEW --curator reject-->  REJECTED  (with rejection_reason)
        REJECTED      --owner revise-->     DRAFT     (must go back through draft)
        APPROVED      --auto-scheduler-->   EXPIRED   (end_date < today)

    Terminal: REJECTED -> cannot re-approve directly (must revise to draft first).
    Terminal: EXPIRED  -> no further transitions.
    """
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class DealType(StrEnum):
    """The kind of discount a deal offers. Open enum; add more as needed."""
    PERCENTAGE_OFF = "percentage_off"   # e.g. 20% off entire bill
    FIXED_AMOUNT = "fixed_amount"       # e.g. $5 off
    BOGO = "bogo"                       # buy one get one
    FREE_ITEM = "free_item"             # free appetizer / dessert with entrée
    BUNDLE = "bundle"                   # multi-item bundle at a set price


class DealAudience(StrEnum):
    """Who a deal is visible to.

    PUBLIC: visible to all eligible users (gated only by their subscription
            tier — for now, a free-tier stub).
    PUSH_ONLY: only visible to users who have subscribed to push notifications
               for this specific restaurant. Per BRD §3.4, push-only deals
               are a Premium-tier feature.
    """
    PUBLIC = "public"
    PUSH_ONLY = "push_only"
