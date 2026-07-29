"""Re-export the shared DeclarativeBase so models only import from `app.models`."""
from app.db.base import Base  # noqa: F401
