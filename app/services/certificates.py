"""CertificateService — upload, download, list, review (Stage 4).

Per BRD §3.2 + §7, halal certificates are stored in Azure Blob Storage.
"Encrypted at rest" is satisfied by Azure SSE (server-side encryption,
AES-256, default-on for every Storage account since 2017) — no client-side
encryption layer needed. This service:

  - Uploads a cert to the dedicated `halalistic-certificates` container.
  - Persists a HalalCertificate row with the blob URL.
  - Exposes a download() method so the test suite (and future admin re-fetch
    endpoints) can verify round-trip integrity.
  - Lists pending + expired certs for the admin queue.

Expiry is checked **on-read** for now (BRD §4 leaves scheduled jobs for
Phase 2). Status-flip-on-expiry is **TODO** — see the EXPIRED path note in
`list_expired()`.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from io import BytesIO
from typing import Optional
from uuid import UUID, uuid4

from azure.storage.blob.aio import BlobServiceClient
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.certifying_body import CertifyingBody
from app.models.enums import CertificateStatus
from app.models.halal_certificate import HalalCertificate
from app.models.user import User

logger = logging.getLogger("halalistic.certificates")

ALLOWED_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
MAX_BYTES = 20 * 1024 * 1024  # 20 MB (cert PDFs can be larger than photos)


class CertificateError(Exception):
    """Base for cert service errors that the API should surface as 4xx."""


class CertifyingBodyNotFound(CertificateError):
    pass


class CertBodyRequired(CertificateError):
    """Neither certifying_body_id nor custom_certifying_body was provided."""


class InvalidDateRange(CertificateError):
    pass


class CertificateService:
    def __init__(
        self,
        connection_string: Optional[str] = None,
        client: Optional[object] = None,
    ):
        """`client` is an injection point for tests (fake BlobServiceClient).
        In production, leave it None and the real Azure client is built.
        """
        self._client = client
        if self._client is None:
            conn = connection_string or settings.azure_blob_connection_string
            if conn:
                self._client = BlobServiceClient.from_connection_string(conn)
        self._container = settings.azure_blob_container_certificates

    def _ext_for(self, content_type: str) -> str:
        return {
            "application/pdf": "pdf",
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
        }[content_type]

    @staticmethod
    def validate_file(data: bytes, content_type: str) -> None:
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise CertificateError(
                f"content_type {content_type!r} not allowed; use one of {sorted(ALLOWED_CONTENT_TYPES)}"
            )
        if len(data) > MAX_BYTES:
            raise CertificateError(f"file too large ({len(data)} bytes; max {MAX_BYTES})")
        if not data:
            raise CertificateError("empty file")

    @staticmethod
    def validate_dates(issue_date: date, expiry_date: Optional[date]) -> None:
        if expiry_date is not None and expiry_date < issue_date:
            raise InvalidDateRange(
                f"expiry_date {expiry_date} is before issue_date {issue_date}"
            )

    async def upload(
        self,
        *,
        cert_id: UUID,
        restaurant_id: UUID,
        data: bytes,
        content_type: str,
    ) -> tuple[str, str]:
        """Push bytes to Azure Blob; return (blob_name, blob_url).

        Azure SSE handles at-rest encryption — see module docstring.
        """
        self.validate_file(data, content_type)
        ext = self._ext_for(content_type)
        blob_name = f"restaurants/{restaurant_id}/certificates/{cert_id}.{ext}"

        if self._client is None:
            blob_url = f"https://placeholder.local/{self._container}/{blob_name}"
            logger.warning(
                "AZURE_BLOB_CONNECTION_STRING not set; storing placeholder URL for cert %s",
                blob_name,
            )
            return blob_name, blob_url

        container = self._client.get_container_client(self._container)
        blob = container.get_blob_client(blob_name)
        await blob.upload_blob(BytesIO(data), overwrite=True, content_type=content_type)
        return blob_name, blob.url

    async def download(self, blob_name: str) -> bytes:
        """Round-trip the bytes back from Azure. Used by tests (DoD-1d) and
        future admin re-fetch endpoints.
        """
        if self._client is None:
            raise CertificateError(
                "Azure Blob not configured; cannot download (this is a dev/test path)"
            )
        container = self._client.get_container_client(self._container)
        blob = container.get_blob_client(blob_name)
        stream = await blob.download_blob()
        return await stream.readall()


# ---------- Service-layer queries (use the AsyncSession) ----------

async def list_certifying_bodies(
    db: AsyncSession, *, active_only: bool = True
) -> list[CertifyingBody]:
    stmt = select(CertifyingBody).order_by(CertifyingBody.name)
    if active_only:
        stmt = stmt.where(CertifyingBody.is_active.is_(True))
    return list((await db.execute(stmt)).scalars().all())


async def get_or_404_cert(db: AsyncSession, cert_id: UUID) -> HalalCertificate:
    cert = await db.get(HalalCertificate, cert_id)
    if cert is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="certificate not found")
    return cert


async def list_pending_for_admin(db: AsyncSession) -> list[dict]:
    """Return all certs awaiting admin review (PENDING status).

    Also includes restaurants whose halal_status='pending' (self-reported
    claims) so the admin sees one unified queue.
    """
    # Pending certs
    cert_q = (
        select(HalalCertificate)
        .where(HalalCertificate.status == CertificateStatus.PENDING)
        .order_by(HalalCertificate.uploaded_at.asc())
    )
    certs = list((await db.execute(cert_q)).scalars().all())

    # Restaurants with halal_status=pending (self_reported) and no pending cert
    from app.models.restaurant import Restaurant
    from app.models.enums import HalalStatus
    rest_q = (
        select(Restaurant)
        .where(Restaurant.halal_status == HalalStatus.PENDING)
        .order_by(Restaurant.updated_at.asc())
    )
    pending_restaurants = list((await db.execute(rest_q)).scalars().all())

    out: list[dict] = []
    for c in certs:
        out.append({
            "kind": "certificate",
            "certificate_id": str(c.id),
            "restaurant_id": str(c.restaurant_id),
            "certifying_body": c.certifying_body.name if c.certifying_body else c.custom_certifying_body,
            "issue_date": c.issue_date.isoformat(),
            "expiry_date": c.expiry_date.isoformat() if c.expiry_date else None,
            "uploaded_at": c.uploaded_at.isoformat(),
            "blob_url": c.blob_url,
        })
    for r in pending_restaurants:
        out.append({
            "kind": "self_reported_claim",
            "restaurant_id": str(r.id),
            "restaurant_name": r.name,
            "halal_status": r.halal_status.value,
            "halal_verification_source": r.halal_verification_source.value,
            "updated_at": r.updated_at.isoformat(),
        })
    return out


async def list_expired(db: AsyncSession) -> list[HalalCertificate]:
    """Return all APPROVED certs whose expiry_date has passed.

    TODO: this is an on-read check. A scheduled job (Phase 2 per BRD §4)
    should also flip each cert's status to EXPIRED and the restaurant's
    halal_status back to 'unverified' / 'pending' for re-verification. Out
    of scope for Stage 4.
    """
    today = date.today()
    stmt = (
        select(HalalCertificate)
        .where(
            and_(
                HalalCertificate.status == CertificateStatus.APPROVED,
                HalalCertificate.expiry_date.is_not(None),
                HalalCertificate.expiry_date < today,
            )
        )
        .order_by(HalalCertificate.expiry_date.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_certs_for_restaurant(
    db: AsyncSession, restaurant_id: UUID,
) -> list[HalalCertificate]:
    """All certs for a single restaurant, newest first. Stage 10 admin
    UI uses this on the restaurant detail page.
    """
    stmt = (
        select(HalalCertificate)
        .where(HalalCertificate.restaurant_id == restaurant_id)
        .order_by(HalalCertificate.uploaded_at.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_active_cert(
    db: AsyncSession, restaurant_id: UUID,
) -> Optional[HalalCertificate]:
    """The most recent APPROVED cert for a restaurant (or None). Used
    by the consumer profile to surface the halal-verified badge
    details. Order is by issue_date desc so the most recently issued
    valid cert wins.
    """
    stmt = (
        select(HalalCertificate)
        .where(
            HalalCertificate.restaurant_id == restaurant_id,
            HalalCertificate.status == "approved",
        )
        .order_by(HalalCertificate.issue_date.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def review_cert(
    db: AsyncSession,
    *,
    cert: HalalCertificate,
    approve: bool,
    admin: User,
    review_notes: Optional[str] = None,
) -> HalalCertificate:
    """Admin approves or rejects a pending cert.

    On approve: cert.status=APPROVED, cert.reviewed_by=admin, cert.reviewed_at=now,
    restaurant.halal_status=VERIFIED, restaurant.halal_verification_source=CERTIFIED.
    On reject: cert.status=REJECTED, cert.reviewed_by=admin, cert.reviewed_at=now,
    notes persisted. Restaurant's status is left alone (stays PENDING or whatever
    the owner set).
    """
    now = datetime.now(timezone.utc)
    cert.reviewed_by_admin_id = admin.id
    cert.reviewed_at = now
    cert.review_notes = review_notes
    if approve:
        cert.status = CertificateStatus.APPROVED
        from app.models.restaurant import Restaurant
        from app.models.enums import HalalStatus, HalalVerificationSource
        r = await db.get(Restaurant, cert.restaurant_id)
        if r is not None:
            r.halal_status = HalalStatus.VERIFIED
            r.halal_verification_source = HalalVerificationSource.CERTIFIED
            r.halal_verified_at = now
            r.halal_verified_by_admin_id = admin.id
    else:
        cert.status = CertificateStatus.REJECTED
    await db.commit()
    await db.refresh(cert)
    return cert
