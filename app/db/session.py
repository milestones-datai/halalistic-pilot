"""Async SQLAlchemy engine and session factory.

Import `get_db` as a FastAPI dependency to get a per-request session.
"""
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# Single engine for the app's lifetime. `pool_pre_ping` reconnects after idle.
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped async session.

    Commits on a clean exit, rolls back on exception. This is the
    FastAPI-idiomatic pattern and is what makes the live (per-request
    session) behavior match the in-test (shared-session) behavior:

      - If the handler returns normally, every `db.add()` / `db.flush()`
        the handler performed is now durable in the DB, so the NEXT
        request (which gets a fresh session) can see the rows.
      - If the handler raises, the transaction is rolled back and
        nothing leaks.

    The auth flow specifically depends on this: `_issue_token_pair`
    only does `db.flush()` because it relies on the dependency to
    commit on exit. Without the post-yield commit, a register-then-
    refresh across two requests would 401 because the refresh-token
    row never persisted.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
