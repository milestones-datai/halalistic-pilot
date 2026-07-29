"""Seed script — Stage 4 populates the certifying-bodies lookup.

Later stages will add cuisines + sample restaurants. Idempotent: safe to
re-run.
"""
import sys
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.certifying_body import CertifyingBody


# (name, slug, country) — common halal certifying bodies per BRD §3.2 examples.
DEFAULT_CERTIFYING_BODIES = [
    ("Islamic Food and Nutrition Council of America", "ifanca", "US"),
    ("Halal Monitoring Services", "hms", "US"),
    ("Islamic Society of Washington Area", "iswa", "US"),
    ("Department of Islamic Development Malaysia (JAKIM)", "jakim", "MY"),
    ("Majelis Ulama Indonesia", "mui", "ID"),
    ("Majlis Ugama Islam Singapura", "muis", "SG"),
    ("Emirates Authority for Standardization", "esma", "AE"),
    ("Saudi Food and Drug Authority", "sfda", "SA"),
]


async def _seed_certifying_bodies() -> int:
    added = 0
    async with AsyncSessionLocal() as s:  # noqa: F821 (name-mangling is fine here)
        for name, slug, country in DEFAULT_CERTIFYING_BODIES:
            existing = (await s.execute(
                select(CertifyingBody).where(CertifyingBody.slug == slug)
            )).scalar_one_or_none()
            if existing is not None:
                continue
            s.add(CertifyingBody(name=name, slug=slug, country=country, is_active=True))
            added += 1
        await s.commit()
    return added


def main() -> int:
    import asyncio
    print(f"[seed] database: {settings.database_url.split('@')[-1] if '@' in settings.database_url else 'local'}")
    added = asyncio.run(_seed_certifying_bodies())
    print(f"[seed] certifying bodies added: {added} (of {len(DEFAULT_CERTIFYING_BODIES)} defaults)")
    print("[seed] cuisines + sample restaurants: TODO (next stage)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
