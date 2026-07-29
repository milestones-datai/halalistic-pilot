# Halalistic — Backend

Houston pilot. FastAPI monolith + Postgres + SQLAlchemy 2 / Alembic (async) +
Azure Blob Storage + Stripe + Google Maps. Custom OAuth2/OIDC, no managed auth.

> **Stage:** 1 — container-ready FastAPI skeleton (no business logic, no auth,
> no models yet). The BRD and Feature Backlog are not committed in this repo
> (kept external, per stakeholder policy).

## Quick start (local dev, native)

Requires Python 3.12+ and a running Postgres (use Docker for the DB only —
the app runs natively for hot-reload).

```bash
# 1. Bring up Postgres
docker compose -f infra/docker-compose.yml up -d

# 2. Create a venv and install deps (editable + dev extras)
python -m venv .venv
# Windows PowerShell:
. .venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate
pip install -e ".[dev]"

# 3. Copy env file
cp .env.example .env

# 4. Run migrations (no-op in Stage 1, but proves Alembic env wiring)
alembic upgrade head

# 5. Run the API
uvicorn app.main:app --reload

# 6. Smoke test
curl http://localhost:8000/health
# {"status":"ok","db":"ok","env":"development"}
```

## Quick start (full container build verification)

```bash
docker build -f infra/Dockerfile -t halalistic-api .
docker compose -f infra/docker-compose.yml up -d
# Then run uvicorn on the host (we keep the app out of compose for hot-reload).
```

## Tests

```bash
pytest
```

## Project layout

```
app/
  main.py              # FastAPI factory + /health (DB ping)
  core/
    config.py          # pydantic-settings, env-driven
    logging.py         # structured logging
  db/
    base.py            # SQLAlchemy 2.0 DeclarativeBase
    session.py         # async engine + sessionmaker
  api/v1/
    router.py          # aggregator (empty in Stage 1)
alembic/
  env.py               # async-aware, imports Base.metadata
  versions/            # no migrations yet
tests/                 # pytest + pytest-asyncio + httpx
scripts/seed.py        # placeholder seed entry point
infra/
  Dockerfile           # multi-stage, python:3.12-slim, non-root
  docker-compose.yml   # Postgres 16 only (app runs natively for dev)
```

## Locked decisions (do not change without flagging)

- Python 3.12 / FastAPI / SQLAlchemy 2 async / Alembic async / asyncpg
- Monolith, not microservices
- Postgres only (no Redis / OpenSearch in pilot — BRD §5.2, §9.2)
- Custom OAuth2/OIDC (not Auth0 / Azure AD B2C — deliberate stakeholder call)
- Stripe Elements / Checkout only (no raw card data on our servers)
- Secrets via env vars / Azure Key Vault — never committed
- Azure Container Apps target (12-factor, env-driven config, no local FS state)

## Stage 1 Definition of Done

- [ ] `docker compose -f infra/docker-compose.yml up -d` brings up Postgres
- [ ] `alembic upgrade head` runs (no-op, but env wiring is proven)
- [ ] `uvicorn app.main:app` boots, `GET /health` returns 200 with `db: ok`
- [ ] `pytest` passes the health smoke test
- [ ] `docker build -f infra/Dockerfile` succeeds

## Open Items still pending (BRD §10)

- #2 — Photo caps per tier
- #3 — Point values per action
- #5 — Frontend framework (React SPA vs Next.js SSR) — **blocks Stage 5**
- #6 — Email provider
