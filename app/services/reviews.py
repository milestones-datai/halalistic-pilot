"""Review service — create + moderate + aggregate (Stage 5).

Per BRD §3.3:
  - Pre-moderation: every new review starts in `PENDING` and does NOT go
    live until an admin approves it. The auto-flag is informational and
    does NOT bypass the admin queue.
  - One review per (reviewer, restaurant) — enforced by the DB unique
    constraint, surfaced as a clean 409 by the service.
  - Aggregate rating on the restaurant profile = AVG(rating) of APPROVED
    reviews only. Pending + rejected reviews are excluded.
  - Max 3 tags per review — enforced here, not in the DB. A 4th tag
    returns 400 with a clear error, NOT silent truncation.
  - Max 3 photos per review — same hard cap, same clear error.

All public service functions return ORM objects or plain dicts (no Pydantic
schemas — those live in the API layer).
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ReviewStatus
from app.models.review import Review, ReviewPhoto, review_tags_table
from app.models.tag import Tag
from app.models.user import User
from app.services import moderation
from app.services.review_photos import ReviewPhotoError, ReviewPhotoService

logger = logging.getLogger("halalistic.reviews")

MAX_TAGS_PER_REVIEW = 3
MAX_PHOTOS_PER_REVIEW = 3
MIN_RATING = 1
MAX_RATING = 5
MAX_BODY_LENGTH = 4000
MAX_INSTAGRAM_URL_LENGTH = 500


# ---- Instagram URL validation (format-only, per Stage 5 brief) ----
_INSTAGRAM_HOST_RE = re.compile(
    r"^https?://(www\.)?(instagram\.com|instagr\.am)/",
    re.IGNORECASE,
)
# Allow paths that look like /p/{code}, /reel/{code}, /reels/{code}, /tv/{code},
# /stories/{user}/{id}. We don't try to validate the code — Instagram's API
# could verify existence, but the brief explicitly says we don't need to.
_INSTAGRAM_PATH_RE = re.compile(
    r"^/((p|reel|reels|tv)/[A-Za-z0-9_-]+|stories/[^/]+/\d+)/?$",
    re.IGNORECASE,
)


def validate_instagram_url(url: str) -> str:
    """Format-only validation. Returns the trimmed URL on success, raises
    HTTPException(400) on a clearly-invalid one. We do NOT call Instagram's
    API to verify the content exists (per Stage 5 brief)."""
    if not url:
        return url
    url = url.strip()
    if len(url) > MAX_INSTAGRAM_URL_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"instagram_embed_url too long (max {MAX_INSTAGRAM_URL_LENGTH} chars)",
        )
    if not _INSTAGRAM_HOST_RE.match(url):
        raise HTTPException(
            status_code=400,
            detail="instagram_embed_url must be a valid instagram.com or instagr.am URL",
        )
    # Extract the path part and check it against the allowed shape.
    path = url.split("//", 1)[-1].split("/", 1)[-1] if "/" in url else ""
    path = "/" + path
    if not _INSTAGRAM_PATH_RE.match(path):
        raise HTTPException(
            status_code=400,
            detail="instagram_embed_url must point to a post, reel, or story (e.g. https://www.instagram.com/p/ABC123/)",
        )
    return url


def validate_rating(rating: int) -> int:
    if not isinstance(rating, int) or rating < MIN_RATING or rating > MAX_RATING:
        raise HTTPException(
            status_code=400,
            detail=f"rating must be an integer between {MIN_RATING} and {MAX_RATING}",
        )
    return rating


def validate_body(body: str) -> str:
    body = (body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="body is required")
    if len(body) > MAX_BODY_LENGTH:
        raise HTTPException(status_code=400, detail=f"body too long (max {MAX_BODY_LENGTH} chars)")
    return body


async def _resolve_tags(db: AsyncSession, tag_ids: list[int]) -> list[Tag]:
    """Resolve a list of tag ids to active Tag objects.

    Raises 400 if any tag id is unknown OR if the resolved set is empty
    after dedupe (we still allow zero tags, so this only fires when ids
    are given but none resolve).
    """
    if not tag_ids:
        return []
    # De-dupe while preserving order.
    seen: set[int] = set()
    unique_ids: list[int] = []
    for t in tag_ids:
        if t not in seen:
            seen.add(t)
            unique_ids.append(t)
    rows = (await db.execute(select(Tag).where(Tag.id.in_(unique_ids)))).scalars().all()
    found = {t.id for t in rows}
    missing = [t for t in unique_ids if t not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"unknown tag ids: {missing}")
    # Filter to active only — deactivated tags cannot be used on NEW reviews.
    active = [t for t in rows if t.is_active]
    if len(active) != len(rows):
        inactive = [t.id for t in rows if not t.is_active]
        raise HTTPException(
            status_code=400,
            detail=f"inactive tag ids cannot be used on new reviews: {inactive}",
        )
    return active


async def create_review(
    db: AsyncSession,
    *,
    restaurant_id: uuid.UUID,
    reviewer: User,
    rating: int,
    body: str,
    tag_ids: list[int],
    photo_uploads: list[tuple[bytes, str]],  # list of (bytes, content_type)
    instagram_embed_url: Optional[str] = None,
) -> Review:
    """Create a new review. Always enters PENDING; pre-moderation gate is
    server-side, not in the DB and not bypassable by the caller.

    `photo_uploads` is a list of (bytes, content_type) tuples; max 3.

    Raises HTTPException on:
      - 400: bad input (rating, body, tags count, photo count, instagram URL)
      - 404: restaurant not found
      - 409: reviewer already has a review for this restaurant
    """
    rating = validate_rating(rating)
    body = validate_body(body)

    if len(tag_ids) > MAX_TAGS_PER_REVIEW:
        # HARD cap. Do NOT silently truncate (DoD #2).
        raise HTTPException(
            status_code=400,
            detail=f"too many tags ({len(tag_ids)}); max is {MAX_TAGS_PER_REVIEW}",
        )
    if len(photo_uploads) > MAX_PHOTOS_PER_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"too many photos ({len(photo_uploads)}); max is {MAX_PHOTOS_PER_REVIEW}",
        )
    if instagram_embed_url:
        instagram_embed_url = validate_instagram_url(instagram_embed_url)

    # Resolve tags (raises 400 on bad tag ids).
    tags = await _resolve_tags(db, tag_ids)

    # Run auto-flag heuristics BEFORE writing the row, so the row's
    # `flagged`/`flag_reasons` reflect what we'd decide right now.
    flagged, reasons = await moderation.evaluate_with_db(
        db, reviewer_id=reviewer.id, body=body,
    )
    reasons_text = "; ".join(reasons) if reasons else None

    review = Review(
        id=uuid.uuid4(),
        restaurant_id=restaurant_id,
        reviewer_id=reviewer.id,
        rating=rating,
        body=body,
        instagram_embed_url=instagram_embed_url,
        moderation_status=ReviewStatus.PENDING.value,
        flagged=flagged,
        flag_reasons=reasons_text,
    )
    review.tags = tags
    db.add(review)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if "uq_review_per_user_restaurant" in str(exc.orig).lower():
            raise HTTPException(
                status_code=409,
                detail="you have already reviewed this restaurant",
            ) from exc
        raise

    # Upload photos after the review row exists so we can use its UUID
    # in the blob name (matches the cert pattern). All uploads are
    # attempted; on the first failure we mark the review as auto-flagged
    # with a flag_reason, but still commit what we have. This keeps
    # honest failures in the moderation queue instead of 500ing.
    photo_service = ReviewPhotoService()
    for idx, (data, content_type) in enumerate(photo_uploads):
        try:
            blob_name, blob_url = await photo_service.upload(
                review_id=review.id, data=data, content_type=content_type,
            )
        except ReviewPhotoError as exc:
            logger.warning("review %s photo %s upload failed: %s", review.id, idx, exc)
            if not review.flagged:
                review.flagged = True
                review.flag_reasons = (
                    f"photo upload failed at index {idx}: {exc}"
                    + (f"; {review.flag_reasons}" if review.flag_reasons else "")
                )
            continue
        db.add(ReviewPhoto(
            id=uuid.uuid4(),
            review_id=review.id,
            blob_name=blob_name,
            blob_url=blob_url,
            content_type=content_type,
            size_bytes=len(data),
            sort_order=idx,
        ))

    await db.commit()
    # Eager-load the relationships we expose on the response so the endpoint
    # can render `r.photos`, `r.tags`, `r.reviewer` without triggering a
    # lazy-load outside the async context.
    await db.refresh(review, attribute_names=["photos", "tags", "reviewer"])
    return review


async def list_pending_for_admin(
    db: AsyncSession, *, flagged_first: bool = True
) -> list[Review]:
    """All PENDING reviews, ordered by `flagged DESC, created_at ASC` so
    admins see flagged (likely-spam) first and oldest-first within each
    bucket. This is the BRD §3.3 "shows flagged distinctly from
    unflagged-but-still-pending" requirement: the ordering, the
    `flagged` boolean, and the `flag_reasons` text together make the
    distinction unambiguous.
    """
    stmt = (
        select(Review)
        .where(Review.moderation_status == ReviewStatus.PENDING.value)
        .order_by(
            Review.flagged.desc() if flagged_first else Review.created_at.asc(),
            Review.created_at.asc(),
        )
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_or_404(db: AsyncSession, review_id: uuid.UUID) -> Review:
    r = await db.get(Review, review_id)
    if r is None:
        raise HTTPException(status_code=404, detail="review not found")
    return r


async def moderate(
    db: AsyncSession,
    *,
    review: Review,
    admin: User,
    approve: bool,
    reason: Optional[str] = None,
) -> Review:
    """Admin approves or rejects a PENDING review. Terminal — once
    approved or rejected, the review is no longer modifiable (matches the
    pre-moderation audit story: a future change would need a new
    resubmission by the diner, not a back-channel admin edit)."""
    if review.moderation_status != ReviewStatus.PENDING.value:
        raise HTTPException(
            status_code=409,
            detail=f"review is already {review.moderation_status}; cannot re-moderate",
        )
    now = datetime.now(timezone.utc)
    review.reviewed_by_admin_id = admin.id
    review.reviewed_at = now
    if approve:
        review.moderation_status = ReviewStatus.APPROVED.value
        review.rejection_reason = None
    else:
        review.moderation_status = ReviewStatus.REJECTED.value
        review.rejection_reason = (reason or "").strip() or None
    await db.commit()
    await db.refresh(review)
    return review


async def aggregate_for_restaurant(
    db: AsyncSession, restaurant_id: uuid.UUID
) -> tuple[Optional[float], int]:
    """Return (avg_rating, count) over APPROVED reviews only.

    Returns (None, 0) when the restaurant has no approved reviews yet.
    """
    stmt = select(func.avg(Review.rating), func.count(Review.id)).where(
        and_(
            Review.restaurant_id == restaurant_id,
            Review.moderation_status == ReviewStatus.APPROVED.value,
        )
    )
    avg, count = (await db.execute(stmt)).one()
    if not count:
        return None, 0
    return float(avg), int(count)


async def list_approved_for_restaurant(
    db: AsyncSession,
    *,
    restaurant_id: uuid.UUID,
    limit: int = 20,
    offset: int = 0,
) -> list[Review]:
    """Public-facing: only approved reviews, newest first."""
    stmt = (
        select(Review)
        .where(
            and_(
                Review.restaurant_id == restaurant_id,
                Review.moderation_status == ReviewStatus.APPROVED.value,
            )
        )
        .order_by(Review.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await db.execute(stmt)).scalars().all())
