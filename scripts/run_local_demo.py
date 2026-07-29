"""Stage 12 demo runner — boot the Halalistic app locally with a real
PostgreSQL backend, seed sample data, and start uvicorn.

Steps:
  1. Connect to PostgreSQL as the `postgres` superuser (local trust auth
     on Windows defaults to no-password).
  2. Create role `halalistic` + database `halalistic` if they don't
     exist.
  3. Run `alembic upgrade head` against the new DB.
  4. Run `scripts/seed.py` to populate demo data (restaurants,
     halal certs, deals, users).
  5. Start `uvicorn` in the background, listening on 0.0.0.0:8000.
  6. Print the local URL + demo credentials.

To stop everything later: kill the uvicorn process.
"""
from __future__ import annotations
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PG_BIN = Path(r"C:\Program Files\PostgreSQL\16\bin")
PSQL = PG_BIN / "psql.exe"

# Ensure the venv python is used
PY = ROOT / ".venv" / "Scripts" / "python.exe"

# Add Postgres + Git to PATH so subprocesses can find psql + alembic
extra = [
    str(PG_BIN),
    r"C:\Program Files\Git\usr\bin",
    r"C:\Program Files\Git\mingw64\bin",
    r"C:\Program Files\Git\cmd",
]
for p in extra:
    if p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")


def run(cmd, *, env=None, check=True, capture=True, cwd=None):
    print(f"\n$ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    r = subprocess.run(cmd, capture_output=capture, text=True,
                       env={**os.environ, **(env or {})}, cwd=cwd or str(ROOT))
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.stderr.strip():
        print("STDERR:", r.stderr.strip()[:400])
    if check and r.returncode != 0:
        sys.exit(r.returncode)
    return r


# ---- 1. Create role + database (idempotent) ----
print("=== creating role + database ===")
# On Windows defaults, postgres superuser has trust auth on local TCP/Unix.
# We need to set a password we can authenticate with from the app.
# Use the asyncpg-friendly URL the app expects.

# Try connecting first — the user may already exist with a known password.
# If not, we'll create with a known password.
PWD = "halalistic_dev_2026"
SQL = f"""
DO $$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'halalistic') THEN
      CREATE ROLE halalistic LOGIN PASSWORD '{PWD}';
   ELSE
      ALTER ROLE halalistic WITH LOGIN PASSWORD '{PWD}';
   END IF;
END
$$;
SELECT 'role ok' AS status;
"""
run([str(PSQL), "-U", "postgres", "-h", "localhost", "-d", "postgres", "-c", SQL])

# Create database if not exists (cannot use IF NOT EXISTS in CREATE DATABASE)
r = subprocess.run(
    [str(PSQL), "-U", "postgres", "-h", "localhost", "-d", "postgres",
     "-tAc", "SELECT 1 FROM pg_database WHERE datname='halalistic'"],
    capture_output=True, text=True,
)
if r.stdout.strip() != "1":
    run([str(PSQL), "-U", "postgres", "-h", "localhost", "-d", "postgres",
         "-c", "CREATE DATABASE halalistic OWNER halalistic"])
else:
    print("database halalistic already exists")

# ---- 2. Write .env with the right DATABASE_URL ----
print("\n=== writing .env ===")
env_path = ROOT / ".env"
db_url = f"postgresql+asyncpg://halalistic:{PWD}@localhost:5432/halalistic"
env_content = (
    f"ENV=development\n"
    f"LOG_LEVEL=INFO\n"
    f"DATABASE_URL={db_url}\n"
    f"SECRET_KEY=dev-secret-do-not-use-in-prod-{int(time.time())}\n"
    f"ACCESS_TOKEN_EXPIRE_MINUTES=60\n"
    f"AZURE_BLOB_CONNECTION_STRING=\n"
    f"STRIPE_SECRET_KEY=\n"
    f"STRIPE_WEBHOOK_SECRET=\n"
    f"GOOGLE_MAPS_API_KEY=\n"
    f"EMAIL_PROVIDER_API_KEY=\n"
    f"VAPID_PRIVATE_KEY=\n"
    f"VAPID_PUBLIC_KEY=\n"
    f"VAPID_SUBJECT=mailto:dev@halalistic.local\n"
    f"APP_PUBLIC_URL=http://localhost:8000\n"
)
env_path.write_text(env_content)
print(f"wrote {env_path} (DATABASE_URL=postgresql://halalistic:***@localhost:5432/halalistic)")

# ---- 3. Alembic upgrade head ----
print("\n=== alembic upgrade head ===")
run([str(PY), "-m", "alembic", "upgrade", "head"])

# ---- 4. Seed ----
print("\n=== seed ===")
seed_script = ROOT / "scripts" / "seed.py"
if seed_script.exists():
    run([str(PY), str(seed_script)])
else:
    print(f"(no {seed_script} — skipping)")

# ---- 5. Start uvicorn in the background ----
print("\n=== starting uvicorn (background) ===")
log_path = ROOT / "_demo_server.log"
log = open(log_path, "w", encoding="utf-8")
proc = subprocess.Popen(
    [str(PY), "-m", "uvicorn", "app.main:app",
     "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"],
    stdout=log, stderr=subprocess.STDOUT,
    cwd=str(ROOT),
    env={**os.environ,
         "ENV": "development",
         "DATABASE_URL": db_url,
         "SECRET_KEY": f"dev-secret-{int(time.time())}",
         "APP_PUBLIC_URL": "http://localhost:8000"},
)
print(f"uvicorn PID: {proc.pid}")
print(f"logs: {log_path}")

# ---- 6. Wait for /health ----
print("\n=== waiting for /health ===")
import urllib.request
ok = False
for i in range(20):
    time.sleep(1)
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2)
        body = r.read().decode()
        print(f"attempt {i+1}: HTTP {r.status}  body={body}")
        if r.status == 200:
            ok = True
            break
    except Exception as e:
        print(f"attempt {i+1}: {type(e).__name__} {e}")

if not ok:
    print("\nuvicorn did not become healthy in 20s. Tail of the log:")
    log.flush()
    print(log_path.read_text()[-2000:])
    sys.exit(1)

print("\n===========================================")
print("  DEMO IS LIVE")
print("===========================================")
print(f"  http://localhost:8000")
print(f"  http://localhost:8000/docs        (Swagger / OpenAPI)")
print(f"  http://localhost:8000/admin/ui    (admin console)")
print(f"  http://localhost:8000/restaurants (discovery)")
print(f"  http://localhost:8000/deals       (deals list)")
print(f"  http://localhost:8000/account     (diner account — needs login)")
print()
print("  Demo credentials:")
print("    admin@halalistic.local / DemoAdmin!1     (platform_admin)")
print("    owner@karachikebab.com  / DemoOwner!1    (restaurant_owner)")
print("    diner@halalistic.local  / DemoDiner!1    (diner)")
print()
print(f"  uvicorn PID: {proc.pid}    (kill with: Stop-Process -Id {proc.pid})")
print("===========================================")
