"""Halal verification + certificate admin endpoints (Stage 4).

Owner-facing cert upload lives in `restaurants.py` (under /restaurants/{id}/halal-certificate).
This router is admin-only: pending queue, cert review, expired certs,
and certifying-body CRUD.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps.auth import require_role
from app.models.certifying_body import CertifyingBody
from app.models.enums import UserRole
from app.models.user import User
from app.services import certificates
from app.services.certificates import CertificateService

router = APIRouter(prefix="/admin", tags=["admin-halal"])


# ---- Schemas ----
class CertifyingBodyIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    slug: str = Field(min_length=2, max_length=120, pattern=r"^[a-z0-9-]+$")
    country: Optional[str] = Field(default=None, max_length=50)


class CertifyingBodyOut(BaseModel):
    id: int
    name: str
    slug: str
    country: Optional[str]
    is_active: bool


class CertReviewIn(BaseModel):
    approve: bool
    review_notes: Optional[str] = Field(default=None, max_length=2000)


class CertSummary(BaseModel):
    id: uuid.UUID
    restaurant_id: uuid.UUID
    certifying_body_name: str  # resolved from FK or custom
    issue_date: str
    expiry_date: Optional[str]
    status: str


# ---- Certifying body CRUD (admin) ----
@router.get("/certifying-bodies", response_model=list[CertifyingBodyOut])
async def list_certifying_bodies(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> list[CertifyingBodyOut]:
    bodies = await certificates.list_certifying_bodies(db)
    return [
        CertifyingBodyOut(
            id=b.id, name=b.name, slug=b.slug, country=b.country, is_active=b.is_active
        )
        for b in bodies
    ]


@router.post(
    "/certifying-bodies",
    response_model=CertifyingBodyOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_certifying_body(
    body: CertifyingBodyIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> CertifyingBodyOut:
    # Uniqueness on name + slug
    from sqlalchemy import select
    existing = (await db.execute(
        select(CertifyingBody).where(
            (CertifyingBody.name == body.name) | (CertifyingBody.slug == body.slug)
        )
    )).scalars().all()
    if existing:
        raise HTTPException(status_code=409, detail="certifying body with that name or slug already exists")
    cb = CertifyingBody(name=body.name, slug=body.slug, country=body.country, is_active=True)
    db.add(cb)
    await db.commit()
    await db.refresh(cb)
    return CertifyingBodyOut(id=cb.id, name=cb.name, slug=cb.slug, country=cb.country, is_active=cb.is_active)


# ---- Admin queue + review ----
@router.get("/halal-certificates/pending", response_model=list[dict])
async def list_pending(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> list[dict]:
    return await certificates.list_pending_for_admin(db)


@router.get("/halal-certificates/expired", response_model=list[CertSummary])
async def list_expired(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> list[CertSummary]:
    rows = await certificates.list_expired(db)
    out = []
    for c in rows:
        name = c.certifying_body.name if c.certifying_body else (c.custom_certifying_body or "?")
        out.append(CertSummary(
            id=c.id, restaurant_id=c.restaurant_id,
            certifying_body_name=name,
            issue_date=c.issue_date.isoformat(),
            expiry_date=c.expiry_date.isoformat() if c.expiry_date else None,
            status=c.status,
        ))
    return out


@router.post(
    "/halal-certificates/{cert_id}/review",
    response_model=CertSummary,
)
async def review_cert(
    cert_id: uuid.UUID,
    body: CertReviewIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_role(UserRole.PLATFORM_ADMIN))],
) -> CertSummary:
    cert = await certificates.get_or_404_cert(db, cert_id)
    if cert.status != "pending":
        raise HTTPException(status_code=409, detail=f"cert status is {cert.status!r}; only pending can be reviewed")
    cert = await certificates.review_cert(
        db, cert=cert, approve=body.approve, admin=admin, review_notes=body.review_notes,
    )
    name = cert.certifying_body.name if cert.certifying_body else (cert.custom_certifying_body or "?")
    return CertSummary(
        id=cert.id, restaurant_id=cert.restaurant_id,
        certifying_body_name=name,
        issue_date=cert.issue_date.isoformat(),
        expiry_date=cert.expiry_date.isoformat() if cert.expiry_date else None,
        status=cert.status,
    )
