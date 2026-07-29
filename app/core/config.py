"""Application settings, loaded from environment variables / .env file.

All config is env-driven (12-factor). No secrets should ever be hardcoded —
use a real `.env` for local dev and Azure Key Vault for deployed envs.
"""
from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env-driven configuration for the Halalistic backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    env: str = Field(default="development", alias="ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    api_v1_prefix: str = Field(default="/api/v1", alias="API_V1_PREFIX")
    project_name: str = Field(default="Halalistic", alias="PROJECT_NAME")

    # --- Database ---
    database_url: str = Field(
        default="postgresql+asyncpg://halalistic:halalistic@localhost:5432/halalistic",
        alias="DATABASE_URL",
    )
    database_url_sync: str = Field(
        default="postgresql://halalistic:halalistic@localhost:5432/halalistic",
        alias="DATABASE_URL_SYNC",
    )

    # --- Security (used in Stage 3+; placeholders for now) ---
    secret_key: str = Field(default="change-me-in-prod", alias="SECRET_KEY")
    access_token_expire_minutes: int = Field(
        default=60, alias="ACCESS_TOKEN_EXPIRE_MINUTES"
    )

    # --- Azure Blob Storage (Stage 5+) ---
    azure_blob_connection_string: str = Field(
        default="", alias="AZURE_BLOB_CONNECTION_STRING"
    )
    azure_blob_container_photos: str = Field(
        default="photos", alias="AZURE_BLOB_CONTAINER_PHOTOS"
    )
    azure_blob_container_deals: str = Field(
        default="deals", alias="AZURE_BLOB_CONTAINER_DEALS"
    )
    azure_blob_container_certificates: str = Field(
        default="certificates", alias="AZURE_BLOB_CONTAINER_CERTIFICATES"
    )
    azure_blob_container_review_photos: str = Field(
        default="review-photos", alias="AZURE_BLOB_CONTAINER_REVIEW_PHOTOS"
    )

    # --- Stripe (Stage 4) ---
    stripe_secret_key: str = Field(default="", alias="STRIPE_SECRET_KEY")
    stripe_webhook_secret: str = Field(default="", alias="STRIPE_WEBHOOK_SECRET")
    stripe_publishable_key: str = Field(default="", alias="STRIPE_PUBLISHABLE_KEY")

    # --- Google Maps (Stage 5) ---
    google_maps_api_key: str = Field(default="", alias="GOOGLE_MAPS_API_KEY")

    # --- OAuth2 / OIDC (Stage 3) ---
    oauth_issuer: str = Field(default="", alias="OAUTH_ISSUER")
    oauth_jwks_url: str = Field(default="", alias="OAUTH_JWKS_URL")
    oauth_audience: str = Field(default="", alias="OAUTH_AUDIENCE")

    # --- CORS (Stage 5) ---
    # Kept as a raw CSV string to avoid pydantic-settings trying to JSON-decode
    # a comma-separated env value. Use `settings.cors_origins_list` for the parsed list.
    cors_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000",
        alias="CORS_ORIGINS",
    )

    @property
    def cors_origins_list(self) -> List[str]:
        """Parsed list of allowed CORS origins."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    # --- Email (Open Item #6) ---
    email_provider_api_key: str = Field(default="", alias="EMAIL_PROVIDER_API_KEY")
    email_from_address: str = Field(
        default="no-reply@halalistic.example", alias="EMAIL_FROM_ADDRESS"
    )

    # --- Email & notifications (Stage 9) ---
    # Email backend selector. "console_log" (default) writes to log + stdout;
    # "azure_acs" uses Azure Communication Services. Anything else falls
    # back to console_log with a warning. The factory in
    # app/services/email/__init__.py is the single source of truth.
    email_backend: str = Field(default="console_log", alias="EMAIL_BACKEND")
    # Azure Communication Services. The PLACEHOLDER literal in either
    # value triggers an automatic fallback to console_log with a loud
    # warning, so a half-configured prod can never silently lose email.
    # See AZURE_DEPLOY_CHECKLIST.md for the deploy runbook.
    azure_communication_connection_string: str = Field(
        default="endpoint=https://PLACEHOLDER.communication.azure.com/;accesskey=PLACEHOLDER",
        alias="AZURE_COMMUNICATION_CONNECTION_STRING",
    )
    azure_communication_sender_address: str = Field(
        default="DoNotReply@PLACEHOLDER.azurecomm.net",
        alias="AZURE_COMMUNICATION_SENDER_ADDRESS",
    )
    # VAPID keys for web push (Stage 9). If left blank, the push service
    # auto-generates them on first boot and writes them to
    # `vapid_keys.json` next to the .env file so they survive restarts.
    # In prod, prefer setting these explicitly (Key Vault / secret
    # manager) so subscribers don't get invalidated on container restart.
    vapid_private_key: str = Field(default="", alias="VAPID_PRIVATE_KEY")
    vapid_public_key: str = Field(default="", alias="VAPID_PUBLIC_KEY")
    # Contact email encoded in the VAPID JWT (per RFC 8292). Use a
    # mailto: or https: URL the push service can contact.
    vapid_claims_email: str = Field(
        default="mailto:ops@halalistic.example", alias="VAPID_CLAIMS_EMAIL",
    )

    # --- Points & referral (Stage 8) ---
    # Per-action point values. All values are env-overridable so the founder
    # can tune without a code deploy. NO MAGIC NUMBERS in service code.
    points_values: dict[str, int] = Field(
        default_factory=lambda: {
            "referral": 500,        # referrer earns this when a referee signs up
            "review": 100,          # reviewer earns this on review approval
            "checkin": 200,         # diner earns this on checkin (capped 1/day/restaurant)
            "min_redemption": 1000, # minimum balance required to request a gift card
        },
        alias="POINTS_VALUES",
    )
    # Admin-toggleable: credit the referrer when the new user has their
    # FIRST APPROVED REVIEW (the "C" trigger). Off by default — flip via
    # env or admin endpoint when the founder wants to enable it. The
    # "A" trigger (email-verified) is always on by design.
    points_referral_credit_on_first_review: bool = Field(
        default=False, alias="POINTS_REFERRAL_CREDIT_ON_FIRST_REVIEW",
    )

    # --- Tier photo caps (BRD §3.4 — values supplied by founder) ---
    # 4 paid-feature tiers. Update the dict if the founder adjusts caps.
    tier_photo_caps: dict[str, int] = Field(
        default_factory=lambda: {
            "free": 2,
            "photo_plus": 4,
            "featured": 6,
            "premium": 10,
        },
        alias="TIER_PHOTO_CAPS",
    )

    # --- Stripe (Stage 7) ---
    # The price IDs below are placeholders for the MVP. Create real
    # Products in your Stripe Dashboard (one per tier, recurring monthly)
    # and replace the placeholder values before going live. The webhook
    # secret is the one Stripe shows you when you add the endpoint URL
    # under Developers → Webhooks.
    stripe_price_restaurant_photo_plus: str = Field(
        default="price_PLACEHOLDER_photo_plus", alias="STRIPE_PRICE_RESTAURANT_PHOTO_PLUS",
    )
    stripe_price_restaurant_featured: str = Field(
        default="price_PLACEHOLDER_featured", alias="STRIPE_PRICE_RESTAURANT_FEATURED",
    )
    stripe_price_restaurant_premium: str = Field(
        default="price_PLACEHOLDER_premium", alias="STRIPE_PRICE_RESTAURANT_PREMIUM",
    )
    stripe_price_user_deals: str = Field(
        default="price_PLACEHOLDER_user_deals", alias="STRIPE_PRICE_USER_DEALS",
    )
    # Public URL of the app — used as the Stripe Checkout success/cancel
    # return URLs. The webhook listener URL is configured in Stripe
    # Dashboard, not in this env.
    app_public_url: str = Field(
        default="http://localhost:8000", alias="APP_PUBLIC_URL",
    )

    # --- Admin UI (Stage 10) ---
    # Signed-cookie secret for the internal admin/curator console. Must be
    # set in prod; defaults to a value derived from SECRET_KEY so the dev
    # box works out of the box but prod MUST override. The console lives
    # at /admin/ui/* and is RBAC-gated to PLATFORM_ADMIN and
    # DEAL_CURATOR (per-role menu visibility inside the UI).
    admin_ui_session_secret: str = Field(
        default="", alias="ADMIN_UI_SESSION_SECRET",
    )
    # Public origin where the admin UI is hosted (used to build absolute
    # links in emails / OG pages, not for routing — the UI is always
    # served from the same FastAPI app).
    admin_ui_url: str = Field(
        default="", alias="ADMIN_UI_URL",
    )

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v_up = v.upper()
        if v_up not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {v!r}")
        return v_up


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor — call this from app code, not Settings() directly."""
    return Settings()


# Module-level singleton. Imported as `from app.core.config import settings`.
settings = get_settings()
