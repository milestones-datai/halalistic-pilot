"""Google Maps Geocoding wrapper.

In production, hits the real API. The service gracefully returns None when
no API key is configured (local dev without a key) — callers should treat
None as "geocoding unavailable" and store the restaurant with NULL lat/lng.
Tests can inject a fake by constructing the class with `client=...` in the
future if needed; for now the service is fully mocked at the caller level.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import googlemaps

from app.core.config import settings

logger = logging.getLogger("halalistic.geocoding")


@dataclass(frozen=True)
class GeocodedAddress:
    latitude: float
    longitude: float
    formatted_address: str
    place_id: Optional[str] = None


class GeocodingService:
    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or settings.google_maps_api_key
        self._client: Optional[googlemaps.Client] = None
        if self._api_key:
            self._client = googlemaps.Client(key=self._api_key)

    def geocode(self, address: str) -> Optional[GeocodedAddress]:
        """Return a GeocodedAddress for the given address, or None on miss/no-key/error."""
        if self._client is None:
            logger.warning(
                "GOOGLE_MAPS_API_KEY not set; geocoding skipped for %r", address
            )
            return None
        try:
            results = self._client.geocode(address)
        except Exception as exc:  # noqa: BLE001 — googlemaps errors are soft
            logger.warning("Geocoding failed for %r: %s", address, exc)
            return None
        if not results:
            return None
        top = results[0]
        loc = top["geometry"]["location"]
        return GeocodedAddress(
            latitude=loc["lat"],
            longitude=loc["lng"],
            formatted_address=top["formatted_address"],
            place_id=top.get("place_id"),
        )
