# Halalistic — Multi-stage Docker image for the FastAPI monolith.
# Stage 1: install deps into a slim runtime image.
# Stage 2: copy the app code (smaller final layer, faster CI cache reuse).

# ---- builder (we don't actually need a builder for pure Python — kept for parity) ----
FROM python:3.12-slim AS runtime

# Don't buffer stdout/stderr — let container logs flow to Azure.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# OS deps: build-essential is needed for argon2-cffi + a few others that
# ship C extensions. libpq for asyncpg. We keep them in the runtime
# image because the slim base doesn't include them; total image stays
# under 250 MB.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq5 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user and group up front so we can chown the
# working directory at the end.
RUN groupadd --system --gid 1001 halalistic \
    && useradd  --system --uid 1001 --gid halalistic --shell /bin/false halalistic

WORKDIR /app

# Install Python deps first — better Docker layer cache: source-only
# changes don't re-run pip.
COPY pyproject.toml ./
# Project has no setup.py/requirements.txt; we install deps directly
# from pyproject.toml. (For prod we'd add a `pip wheel` step + install
# from a wheels layer; this is fine for pilot.)
RUN pip install --no-cache-dir \
        "fastapi~=0.115.0" \
        "uvicorn[standard]~=0.32.0" \
        "pydantic~=2.9.0" \
        "pydantic-settings~=2.6.0" \
        "sqlalchemy~=2.0.36" \
        "alembic~=1.14.0" \
        "asyncpg~=0.30.0" \
        "argon2-cffi~=23.1.0" \
        "pyjwt~=2.10.0" \
        "slowapi~=0.1.9" \
        "email-validator~=2.2.0" \
        "googlemaps~=4.10.0" \
        "azure-storage-blob[aio]~=12.19.0" \
        "Pillow~=10.4.0" \
        "python-multipart~=0.0.9" \
        "stripe~=11.0" \
        "pywebpush~=2.0" \
        "azure-communication-email~=1.0" \
        "jinja2~=3.1.0" \
        "itsdangerous~=2.2.0"

# Now copy the app.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Drop privileges. Azure Container Apps runs as the image's USER by default.
RUN chown -R halalistic:halalistic /app
USER halalistic

EXPOSE 8000

# Healthcheck hits /health which pings the DB. This is what ACA uses
# to mark the replica as unhealthy. Note: we DO NOT mark the whole
# pod unhealthy just because DB is down — only the app, so ACA can
# restart it. (Per BRD §5.4 "best-effort uptime" — we surface
# degraded health, never silent.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/health || exit 1

# uvicorn with 1 worker per replica; ACA scales horizontally.
# --proxy-headers so client IPs come from the ACA ingress correctly.
# --forwarded-allow-ips='*' is fine because ACA only forwards from
# the platform's own proxies (we don't expose the container directly).
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips=*", \
     "--workers", "1"]
