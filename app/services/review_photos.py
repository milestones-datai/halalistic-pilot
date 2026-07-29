"""ReviewPhotoService — Azure Blob push for review photos (Stage 5).

Separate from the restaurant photo service because:
  - Different access pattern (review photos are read alongside the review,
    not browsed as a gallery).
  - Different retention semantics (could differ in the future).
  - Different RBAC (public, same as review text — but isolated in case we
    later decide review photos should be admin-gated on flag).
"""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional
from uuid import UUID, uuid4

from azure.storage.blob.aio import BlobServiceClient
from PIL import Image

from app.core.config import settings

logger = logging.getLogger("halalistic.review_photos")

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_BYTES = 10 * 1024 * 1024          # 10 MB per photo (matches restaurant photos)
MAX_DIMENSION = 4096


class ReviewPhotoError(Exception):
    """Base for review-photo service errors that the API should surface as 4xx."""


class ReviewPhotoService:
    def __init__(self, connection_string: Optional[str] = None):
        self._conn = connection_string or settings.azure_blob_connection_string
        self._container = settings.azure_blob_container_review_photos
        self._client: Optional[BlobServiceClient] = None
        if self._conn:
            self._client = BlobServiceClient.from_connection_string(self._conn)

    @staticmethod
    def validate_image(data: bytes, content_type: str) -> tuple[int, int]:
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise ReviewPhotoError(
                f"content_type {content_type!r} not allowed; use one of {sorted(ALLOWED_CONTENT_TYPES)}"
            )
        if len(data) > MAX_BYTES:
            raise ReviewPhotoError(f"file too large ({len(data)} bytes; max {MAX_BYTES})")
        try:
            img = Image.open(BytesIO(data))
            img.verify()
            img = Image.open(BytesIO(data))
            width, height = img.size
        except Exception as exc:
            raise ReviewPhotoError(f"not a valid image: {exc}") from exc
        if width > MAX_DIMENSION or height > MAX_DIMENSION:
            raise ReviewPhotoError(f"image too large ({width}x{height}; max {MAX_DIMENSION}px)")
        return width, height

    async def upload(
        self,
        *,
        review_id: UUID,
        data: bytes,
        content_type: str,
    ) -> tuple[str, str]:
        """Validate + push to Azure (or stub); return (blob_name, blob_url)."""
        self.validate_image(data, content_type)
        ext = content_type.split("/")[-1]
        blob_name = f"reviews/{review_id}/{uuid4()}.{ext}"

        if self._client is None:
            blob_url = f"https://placeholder.local/{self._container}/{blob_name}"
            logger.warning(
                "AZURE_BLOB_CONNECTION_STRING not set; storing placeholder URL for %s",
                blob_name,
            )
            return blob_name, blob_url

        container = self._client.get_container_client(self._container)
        blob = container.get_blob_client(blob_name)
        await blob.upload_blob(BytesIO(data), overwrite=True, content_type=content_type)
        return blob_name, blob.url
