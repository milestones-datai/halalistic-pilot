"""Daily cron entry-point: mark approved deals past their end_date as expired.

Per BRD §3.5, expired deals stop appearing in the public active listing
but remain queryable for analytics. The public listing also filters
defensively on `end_date >= today` so a stalled cron doesn't leak
expired deals.

Wire this into cron (Linux) / Task Scheduler (Windows):

    # Every day at 00:05 server time
    5 0 * * *  cd /path/to/halalistic && .venv/bin/python -m scripts.expire_deals
"""
from __future__ import annotations

import asyncio
import logging

from app.db.session import SessionLocal
from app.services.deals import expire_old_deals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("halalistic.expire_deals")


async def main() -> int:
    async with SessionLocal() as db:
        n = await expire_old_deals(db)
        logger.info("expire_deals: %d deal(s) transitioned to expired", n)
        return n


if __name__ == "__main__":
    asyncio.run(main())
