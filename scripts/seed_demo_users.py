"""Stage 12 — create demo users against the running local uvicorn.

After run_local_demo.py has booted the app + seeded halal cert bodies
+ cuisines, this script POSTs /api/v1/auth/register for three demo
users (admin, owner, diner) so you can log in immediately and click
around the four role workflows.

Idempotent: if a user already exists, we skip and just print a note.
"""
import json
import sys
import time
import urllib.request

API = "http://127.0.0.1:8000/api/v1"

DEMO_USERS = [
    {"email": "owner@karachikebab.com", "password": "DemoOwner!1",
     "display_name": "Imran (Owner)", "role": "restaurant_owner"},
    {"email": "diner@halalistic.com", "password": "DemoDiner!1",
     "display_name": "Ayesha (Diner)", "role": "diner"},
]


def post(path, body, token=None):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API + path, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def wait_for_health(retries=10):
    for i in range(retries):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(1)
    sys.exit("server didn't come up in 10s")


print("=== waiting for /health ===")
wait_for_health()
print("ok\n")

for u in DEMO_USERS:
    print(f"-> register {u['email']:32s}", end=" ")
    status, body = post("/auth/register", {
        "email": u["email"],
        "password": u["password"],
        "display_name": u["display_name"],
        "role": u["role"],
    })
    if status == 201:
        uid = body.get("user", body).get("id", "?")[:8]
        print(f"created (id={uid}...)")
    elif status == 409:
        print("already exists (ok)")
    else:
        print(f"FAIL: HTTP {status}  body={body}")
        # Don't bail — show all failures then exit
        sys.exit(1)

# Also create a sample restaurant for the owner so they have something to manage
print("\n-> login as owner to create a sample restaurant")
status, body = post("/auth/login", {
    "email": "owner@karachikebab.com", "password": "DemoOwner!1",
})
if status != 200:
    print(f"   login failed: HTTP {status}  body={body}")
    sys.exit(1)
owner_token = body["access_token"]

status, body = post("/restaurants", {
    "name": "Karachi Kebab House",
    "slug": "karachi-kebab-house",
    "address_line": "123 Main St",
    "city": "Houston", "state": "TX", "postal_code": "77002",
    "cuisine_tags": ["Pakistani", "BBQ"],
    "menu_items": [{"name": "Seekh Kebab", "price_cents": 1295, "description": "Hand-ground lamb"}],
    "price_range": "2",
}, token=owner_token)
if status == 201:
    print(f"   sample restaurant created (id={body['id'][:8]}...)")
elif status == 409:
    print("   sample restaurant already exists (ok)")
else:
    print(f"   restaurant create: HTTP {status}  body={body}")

print("\n===========================================")
print("  DEMO CREDENTIALS READY")
# Also create the admin user directly in the DB — /auth/register
# rejects the platform_admin role (intentional security boundary;
# admins must be promoted, not self-registered). We use the auth
# service's password hasher to do it the same way the register
# endpoint would.
print("\n-> create admin user directly (platform_admin role isn't self-registerable)")
import os, subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
admin_create = r"""
import asyncio
from sqlalchemy import select, update
from app.db.session import AsyncSessionLocal
from app.services.auth_service import register_user, RegisterInput
from app.models.user import User, UserRole
from app.core.security import hash_password

async def main():
    async with AsyncSessionLocal() as db:
        # Check if admin already exists
        r = await db.execute(select(User).where(User.email == "admin@halalistic.com"))
        existing = r.scalar_one_or_none()
        if existing:
            print(f"   admin already exists (id={existing.id}, role={existing.role.value})")
            if existing.role != UserRole.PLATFORM_ADMIN:
                # promote
                existing.role = UserRole.PLATFORM_ADMIN
                await db.commit()
                print(f"   promoted to platform_admin")
            return
        # Create via the public path (forces role=restaurant_owner at service layer),
        # then update role to platform_admin via direct UPDATE. This is the
        # documented "seed-time admin" path.
        u = await register_user(db, RegisterInput(
            email="admin@halalistic.com",
            password="DemoAdmin!1",
            display_name="Rashida (Admin)",
            role=UserRole.RESTAURANT_OWNER,  # accepted by service; we promote next
        ))
        # Promote to platform_admin
        u.role = UserRole.PLATFORM_ADMIN
        await db.commit()
        await db.refresh(u)
        print(f"   admin created + promoted: id={u.id}, role={u.role.value}")

asyncio.run(main())
"""
r = subprocess.run([str(PY), "-c", admin_create], capture_output=True, text=True,
                   cwd=str(ROOT))
print(r.stdout.strip() or r.stderr.strip())


print("\n===========================================")
print("  DEMO CREDENTIALS READY")
print("===========================================")
print("  platform_admin:")
print("    admin@halalistic.com / DemoAdmin!1")
print("  restaurant_owner:")
print("    owner@karachikebab.com / DemoOwner!1")
print("    (owns 'Karachi Kebab House' in Houston)")
print("  diner:")
print("    diner@halalistic.com / DemoDiner!1")
print()
print("  Open in browser:")
print("    http://localhost:8000                  ← home")
print("    http://localhost:8000/web/login        ← log in")
print("    http://localhost:8000/admin/ui         ← admin console (after admin login)")
print("    http://localhost:8000/restaurants      ← browse as diner (after diner login)")
print("    http://localhost:8000/docs             ← API explorer")
print("===========================================")
