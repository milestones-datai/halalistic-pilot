"""Unit tests for restaurant service + photo service.

Geocoding and Azure Blob are not used at this layer (they're called by
service code with the service being injected as a dependency), so we
don't mock them here — the integration tests cover those paths.
"""
import io
import uuid
from unittest.mock import patch

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.models.enums import (
    HalalStatus,
    HalalVerificationSource,
    PriceRange,
    RestaurantTier,
    UserRole,
)
from app.models.restaurant import Cuisine, Photo, Restaurant
from app.services.photos import PhotoService
from app.services import restaurant_service


# ---- Photo validation (pure unit test) ----
def _make_png_bytes(width: int = 100, height: int = 100) -> bytes:
    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_validate_image_accepts_real_png():
    data = _make_png_bytes(width=100, height=100)
    w, h = PhotoService.validate_image(data, "image/png")
    assert w == 100 and h == 100


def test_validate_image_rejects_garbage():
    with pytest.raises(ValueError, match="not a valid image"):
        PhotoService.validate_image(b"not an image at all", "image/jpeg")


def test_validate_image_rejects_oversize():
    with pytest.raises(ValueError, match="file too large"):
        PhotoService.validate_image(b"x" * (10 * 1024 * 1024 + 1), "image/jpeg")


def test_validate_image_rejects_huge_dimensions():
    data = _make_png_bytes(width=5000, height=5000)
    with pytest.raises(ValueError, match="image too large"):
        PhotoService.validate_image(data, "image/png")


def test_validate_image_rejects_bad_content_type():
    with pytest.raises(ValueError, match="not allowed"):
        PhotoService.validate_image(b"x", "application/pdf")


def test_tier_caps_match_settings():
    """Sanity check that the static config and the model are in sync."""
    assert PhotoService.cap_for_tier(RestaurantTier.FREE) == 2
    assert PhotoService.cap_for_tier(RestaurantTier.PHOTO_PLUS) == 4
    assert PhotoService.cap_for_tier(RestaurantTier.FEATURED) == 6
    assert PhotoService.cap_for_tier(RestaurantTier.PREMIUM) == 10
