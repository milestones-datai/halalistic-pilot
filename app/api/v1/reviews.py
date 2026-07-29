"""Public review routes + Diner review submission (Stage 5).

  - `POST /api/v1/restaurants/{restaurant_id}/reviews` — Diner only,
    multipart (text fields + up to 3 photos). Always enters PENDING.
  - `GET  /api/v1/restaurants/{restaurant_id}/reviews` — public,
    paginated, APPROVED only.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps.auth import get_current_user, require_role
from app.models.enums import UserRole
from app.models.review import Review
from app.models.user import User
from app.services import reviews as review_service

router = APIRouter(tags=["reviews"])


# ---- DTOs ----
class TagBriefOut(BaseModel):
    id: int
    name: str
    slug: str

    class Config:
        from_attributes = True


class ReviewPhotoOut(BaseModel):
    id: uuid.UUID
    blob_url: str
    content_type: str
    size_bytes: int
    sort_order: int


class ReviewerBriefOut(BaseModel):
    id: uuid.UUID
    display_name: str


class ReviewOut(BaseModel):
    id: uuid.UUID
    restaurant_id: uuid.UUID
    rating: int
    body: str
    instagram_embed_url: Optional[str] = None
    moderation_status: str
    flagged: bool
    flag_reasons: Optional[str] = None
    rejection_reason: Optional[str] = None
    created_at: datetime
    reviewed_at: Optional[datetime] = None
    tags: list[TagBriefOut] = []
    photos: list[ReviewPhotoOut] = []
    reviewer: ReviewerBriefOut


# ---- Public: approved reviews for a restaurant ----
@router.get("/restaurants/{restaurant_id}/reviews", response_model=list[ReviewOut])
async def list_approved_reviews(
    restaurant_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[ReviewOut]:
    rows = await review_service.list_approved_for_restaurant(
        db, restaurant_id=restaurant_id, limit=limit, offset=offset,
    )
    return [_review_to_out(r) for r in rows]


# ---- Diner: submit a review ----
@router.post(
    "/restaurants/{restaurant_id}/reviews",
    response_model=ReviewOut,
    status_code=status.HTTP_201_CREATED,
)
async def submit_review(
    restaurant_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    diner: Annotated[User, Depends(require_role(UserRole.DINER, UserRole.PLATFORM_ADMIN))],
    rating: int = Form(..., ge=1, le=5),
    body: str = Form(..., min_length=1, max_length=4000),
    tag_ids: Optional[str] = Form(default=None),  # JSON list of ints, e.g. "[1,2,3]"
    instagram_embed_url: Optional[str] = Form(default=None, max_length=500),
    file0: Optional[UploadFile] = File(default=None),
    file1: Optional[UploadFile] = File(default=None),
    file2: Optional[UploadFile] = File(default=None),
) -> ReviewOut:
    """Diner submits a review. Always enters PENDING (pre-moderation)."""
    import json
    tag_id_list: list[int] = []
    if tag_ids:
        try:
            parsed = json.loads(tag_ids)
            if not isinstance(parsed, list) or not all(isinstance(t, int) for t in parsed):
                raise ValueError("tag_ids must be a JSON list of integers")
            tag_id_list = parsed
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid tag_ids: {exc}")

    uploads: list[tuple[bytes, str]] = []
    for f in (file0, file1, file2):
        if f is None:
            continue
        data = await f.read()
        if not data:
            continue
        uploads.append((data, f.content_type or "image/jpeg"))

    review = await review_service.create_review(
        db,
        restaurant_id=restaurant_id,
        reviewer=diner,
        rating=rating,
        body=body,
        tag_ids=tag_id_list,
        photo_uploads=uploads,
        instagram_embed_url=instagram_embed_url,
    )
    return _review_to_out(review)


# ---- Admin: pending queue + moderation decision ----
# (Defined alongside admin endpoints for clarity. Kept on this router so the
# review surfaces all hang off the same tag in the OpenAPI doc.)
@router.get("/admin/reviews/pending", response_model=list[ReviewOut])
async def admin_list_pending_reviews(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> list[ReviewOut]:
    rows = await review_service.list_pending_for_admin(db)
    return [_review_to_out(r) for r in rows]


class ModerateIn(BaseModel):
    approve: bool
    reason: Optional[str] = None


@router.post("/admin/reviews/{review_id}/moderate", response_model=ReviewOut)
async def admin_moderate_review(
    review_id: uuid.UUID,
    body: ModerateIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> ReviewOut:
    review = await review_service.get_or_404(db, review_id)
    review = await review_service.moderate(
        db, review=review, admin=admin, approve=body.approve, reason=body.reason,
    )
    # Stage 8: on approval, credit the reviewer 100 points + fire the
    # referral "C" trigger for the reviewer's referrer (if C is on
    # AND the reviewer has a referrer).
    if body.approve:
        from app.services import points as points_service
        from app.services import referrals as referrals_service
        await points_service.credit_for_review(
            db, reviewer_id=review.reviewer_id, review_id=review.id,
        )
        await referrals_service.credit_referral_if_eligible(
            db, referred_user_id=review.reviewer_id,
        )
    return _review_to_out(review)


# ---- Admin: tag CRUD ----
class CreateTagIn(BaseModel):
    name: str
    slug: Optional[str] = None
    category: Optional[str] = None


class UpdateTagIn(BaseModel):
    is_active: bool


class AdminTagOut(BaseModel):
    id: int
    name: str
    slug: str
    category: Optional[str] = None
    is_active: bool

    class Config:
        from_attributes = True


@router.get("/admin/tags", response_model=list[AdminTagOut])
async def admin_list_tags(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> list[AdminTagOut]:
    from app.services import tags as tag_service
    rows = await tag_service.list_tags(db, active_only=False)
    return [AdminTagOut.model_validate(t) for t in rows]


@router.post(
    "/admin/tags",
    response_model=AdminTagOut,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_tag(
    body: CreateTagIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> AdminTagOut:
    from app.services import tags as tag_service
    t = await tag_service.create_tag(
        db, name=body.name, slug=body.slug, category=body.category,
    )
    return AdminTagOut.model_validate(t)


@router.patch("/admin/tags/{tag_id}", response_model=AdminTagOut)
async def admin_toggle_tag(
    tag_id: int,
    body: UpdateTagIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> AdminTagOut:
    from app.services import tags as tag_service
    t = await tag_service.set_tag_active(db, tag_id=tag_id, is_active=body.is_active)
    return AdminTagOut.model_validate(t)


# ---- helpers ----
def _review_to_out(r: Review) -> ReviewOut:
    return ReviewOut(
        id=r.id,
        restaurant_id=r.restaurant_id,
        rating=r.rating,
        body=r.body,
        instagram_embed_url=r.instagram_embed_url,
        moderation_status=r.moderation_status,
        flagged=r.flagged,
        flag_reasons=r.flag_reasons,
        rejection_reason=r.rejection_reason,
        created_at=r.created_at,
        reviewed_at=r.reviewed_at,
        tags=[TagBriefOut.model_validate(t) for t in r.tags],
        photos=[
            ReviewPhotoOut(
                id=p.id, blob_url=p.blob_url, content_type=p.content_type,
                size_bytes=p.size_bytes, sort_order=p.sort_order,
            )
            for p in r.photos
        ],
        reviewer=ReviewerBriefOut(id=r.reviewer_id, display_name=r.reviewer.display_name)
        if r.reviewer is not None
        else ReviewerBriefOut(id=r.reviewer_id, display_name="(deleted)"),
    )
