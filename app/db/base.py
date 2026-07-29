"""SQLAlchemy 2.0 declarative base.

All ORM models in later stages must inherit from `Base` so that Alembic's
`target_metadata` (set in `alembic/env.py`) sees them and autogenerate works.
"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Common base for all ORM models in Halalistic."""
    pass
