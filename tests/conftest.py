"""Pytest fixtures.

Two test-data strategies here, both proven and simpler than nested-savepoint
wrestling with asyncpg's "another operation in progress" rule:

1. The `db` fixture hands out a fresh AsyncSession bound to the live engine.
   Each test commits freely; the autouse `_clean_tables` fixture truncates
   the auth-related tables between tests so we never collide on (email, id).
2. The `client` fixture overrides the app's get_db to yield that same session,
   so what the app commits is what the test sees.

For the internal admin/curator console (Stage 10), tests use the
`X-Test-User-Id` request header to simulate a logged-in admin/curator.
The corresponding middleware is registered in app/main.py at app
creation time, gated by `settings.env == "test"`, so it never reaches
production.

Also resets the slowapi in-memory rate-limit storage before every test so
rate-limit tests don't share budget with other tests in the session.
"""
import os
from typing import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("ENV", "test")

from app.core.config import settings  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402

# Same DB, separate engine. NullPool so every autouse TRUNCATE / every per-test
# session gets a brand-new connection — prevents "another operation is in
# progress" errors that crop up when asyncpg's connection pool returns a
# connection still mid-operation.
_test_engine = create_async_engine(
    settings.database_url, echo=False, future=True, poolclass=NullPool,
)
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture(autouse=True)
async def _reset_rate_limit_storage():
    try:
        limiter._storage.reset()
    except Exception:
        pass
    yield


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    """Truncate all auth + restaurant + menu + cert tables between tests so we get a clean slate."""
    async with _test_engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE TABLE "
        "push_subscriptions, gift_card_redemptions, points_transactions, checkins, "
        "user_billing_subscriptions, restaurant_billing_subscriptions, "
        "restaurant_push_subscriptions, deals, "
        "review_tags, review_photos, reviews, tags, "
        "halal_certificates, certifying_bodies, "
        "menu_item_variants, menu_items, menu_subcategories, menu_categories, "
        "photos, restaurant_cuisines, restaurants, cuisines, "
        "password_reset_tokens, refresh_tokens, users "
            "RESTART IDENTITY CASCADE"
        ))
    yield


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """A per-test session bound to the real engine.

    The app's get_db dependency is overridden (by the `client` fixture) to
    yield THIS session, so app commits and test reads share state. We rely
    on the autouse TRUNCATE to keep tests isolated.
    """
    async with _TestSession() as session:
        yield session


@pytest_asyncio.fixture
async def client(db) -> AsyncGenerator[AsyncClient, None]:
    def _override_get_db():
        yield db
    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
