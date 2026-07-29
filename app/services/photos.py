"""Azure Blob photo upload + tier-cap enforcement (Stage 3).

In production, pushes the bytes to Azure Blob Storage and persists a Photo
row with the blob URL. If no Azure connection string is configured (local
dev without storage), still returns a Photo row with a placeholder URL so
the rest of the flow works.
"""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional
from uuid import UUID, uuid4

from azure.storage.blob.aio import BlobServiceClient
from PIL import Image

from app.core.config import settings
from app.models.enums import RestaurantTier
from app.models.restaurant import Photo, Restaurant

logger = logging.getLogger("halalistic.photos")

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_BYTES = 10 * 1024 * 1024       # 10 MB
MAX_DIMENSION = 4096              # pixels on the long edge


class PhotoCapExceeded(Exception):
    """Tier photo cap reached; surface as 409 to the API."""

    def __init__(self, current_count: int, cap: int, tier: RestaurantTier):
        self.current_count = current_count
        self.cap = cap
        self.tier = tier
        super().__init__(
            f"tier {tier.value} allows {cap} photos; restaurant already has {current_count}"
        )


class PhotoService:
    def __init__(self, connection_string: Optional[str] = None):
        self._conn = connection_string or settings.azure_blob_connection_string
        self._container = settings.azure_blob_container_photos
        self._client: Optional[BlobServiceClient] = None
        if self._conn:
            self._client = BlobServiceClient.from_connection_string(self._conn)

    @staticmethod
    def cap_for_tier(tier: RestaurantTier) -> int:
        """Photo cap for a given tier (read from settings.tier_photo_caps)."""
        return settings.tier_photo_caps.get(tier.value, 0)

    @staticmethod
    def validate_image(data: bytes, content_type: str) -> tuple[int, int]:
        """Decode + size-check the bytes. Returns (width, height). Raises ValueError."""
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise ValueError(
                f"content_type {content_type!r} not allowed; use one of {sorted(ALLOWED_CONTENT_TYPES)}"
            )
        if len(data) > MAX_BYTES:
            raise ValueError(f"file too large ({len(data)} bytes; max {MAX_BYTES})")
        try:
            img = Image.open(BytesIO(data))
            img.verify()
            img = Image.open(BytesIO(data))  # verify() consumes the file
            width, height = img.size
        except Exception as exc:
            raise ValueError(f"not a valid image: {exc}") from exc
        if width > MAX_DIMENSION or height > MAX_DIMENSION:
            raise ValueError(f"image too large ({width}x{height}; max {MAX_DIMENSION}px)")
        return width, height

    async def upload(
        self,
        *,
        restaurant: Restaurant,
        data: bytes,
        content_type: str,
        caption: Optional[str] = None,
    ) -> Photo:
        """Validate, upload to Azure (or stub), return the new Photo row (not yet committed)."""
        width, height = self.validate_image(data, content_type)

        blob_name = f"restaurants/{restaurant.id}/{uuid4()}.{content_type.split('/')[-1]}"
        if self._client is None:
            blob_url = f"https://placeholder.local/{self._container}/{blob_name}"
            logger.warning(
                "AZURE_BLOB_CONNECTION_STRING not set; storing placeholder URL for %s",
                blob_name,
            )
        else:
            container = self._client.get_container_client(self._container)
            blob = container.get_blob_client(blob_name)
            await blob.upload_blob(BytesIO(data), overwrite=True, content_type=content_type)
            blob_url = blob.url

        return Photo(
            id=uuid4(),
            restaurant_id=restaurant.id,
            blob_name=blob_name,
            blob_url=blob_url,
            content_type=content_type,
            size_bytes=len(data),
            width=width,
            height=height,
            caption=caption,
        )
