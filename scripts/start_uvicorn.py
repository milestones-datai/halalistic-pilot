"""Start uvicorn in the background, with the correct env vars, and
verify it serves the right routes.
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
log_path = ROOT / "_demo_server.log"

db_url = "postgresql+asyncpg://halalistic:halalistic_dev_2026@localhost:5432/halalistic"

# Open the log file (truncate)
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
print(f"started uvicorn PID {proc.pid}")
print(f"log: {log_path}")

# Wait for /health
ok = False
for i in range(20):
    time.sleep(1)
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2)
        body = r.read().decode()
        if r.status == 200:
            print(f"healthy after {i+1}s: {body}")
            ok = True
            break
    except Exception as e:
        print(f"  {i+1}s: {type(e).__name__}")

if not ok:
    print("did not become healthy in 20s")
    sys.exit(1)

# Smoke test the new routes
print("\n=== smoke test ===")
import urllib.error
for path in ["/", "/restaurants", "/deals", "/admin/ui/login", "/web/login", "/docs"]:
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=5)
        body = r.read()[:80].decode(errors="replace")
        print(f"  GET {path:24s} -> {r.status}  {body!r}")
    except urllib.error.HTTPError as e:
        body = e.read()[:80].decode(errors="replace")
        print(f"  GET {path:24s} -> {e.code}  {body!r}")

print(f"\nPID: {proc.pid}    (kill with: taskkill /F /PID {proc.pid})")
print("Demo URL: http://localhost:8000")
