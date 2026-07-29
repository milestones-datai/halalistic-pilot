"""Email message dataclass — kept in its own module to avoid circular
imports between `app.services.email` (the factory) and the concrete
backends (`console_log`, `azure_acs`).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    body: str
    # Optional HTML body. Backends prefer html when present, fall back to
    # plain text otherwise.
    html: str | None = None
