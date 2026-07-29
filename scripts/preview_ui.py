"""Stage 11 — visual preview.

Renders the 14 Jinja2 templates with sample data into a _preview/
directory, then takes Playwright screenshots of each page so the
founder can eyeball the UI without needing Postgres + uvicorn.

Run: .venv/Scripts/python.exe scripts/preview_ui.py
"""
from __future__ import annotations
import asyncio
import os
import sys
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "app" / "web" / "templates"
OUT = ROOT / "_preview"
SHOTS = OUT / "shots"
OUT.mkdir(exist_ok=True)
SHOTS.mkdir(exist_ok=True)


def build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TPL)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env


# ---- Sample data: realistic enough to show the layout ----
SAMPLE_DEALS = [
    {"id": "d1", "title": "20% off family platters",
     "description": "Friday and Saturday only — bring the family.",
     "deal_type": "percentage_off",
     "end_date": date.today() + timedelta(days=7),
     "restaurant_id": "r1", "restaurant_name": "Karachi Kebab House",
     "restaurant_city": "Houston"},
    {"id": "d2", "title": "Buy 1 chicken tikka, get 1 free",
     "description": "Lunch buffet, dine-in only.",
     "deal_type": "bogo",
     "end_date": date.today() + timedelta(days=3),
     "restaurant_id": "r2", "restaurant_name": "Al-Madinah Grill",
     "restaurant_city": "Sugar Land"},
    {"id": "d3", "title": "$5 off orders $30+",
     "description": "Pickup or delivery via our app.",
     "deal_type": "fixed_amount",
     "end_date": date.today() + timedelta(days=14),
     "restaurant_id": "r3", "restaurant_name": "Marrakech Cafe",
     "restaurant_city": "Houston"},
]

SAMPLE_RESTAURANTS = [
    {"id": "r1", "name": "Karachi Kebab House", "slug": "karachi-kebab-house",
     "cuisine_tags": ["Pakistani", "BBQ"], "price_range": "2",
     "city": "Houston", "halal_status_value": "verified", "tier_value": "photo_plus",
     "average_rating": 4.6, "review_count": 142, "is_claimed": True,
     "short_description": "Family-run since 2008. Hand-ground seekh kebabs over charcoal."},
    {"id": "r2", "name": "Al-Madinah Grill", "slug": "al-madinah-grill",
     "cuisine_tags": ["Lebanese", "Mediterranean"], "price_range": "2",
     "city": "Sugar Land", "halal_status_value": "verified", "tier_value": "free",
     "average_rating": 4.3, "review_count": 87, "is_claimed": True,
     "short_description": "Authentic Lebanese plates with house-made hummus."},
    {"id": "r3", "name": "Marrakech Cafe", "slug": "marrakech-cafe",
     "cuisine_tags": ["Moroccan"], "price_range": "1",
     "city": "Houston", "halal_status_value": "pending", "tier_value": "free",
     "average_rating": 4.8, "review_count": 56, "is_claimed": False,
     "short_description": "Tagines, mint tea, and the best pastilla in the Med Center."},
    {"id": "r4", "name": "Istanbul Street Eats", "slug": "istanbul-street-eats",
     "cuisine_tags": ["Turkish"], "price_range": "1",
     "city": "Houston", "halal_status_value": "verified", "tier_value": "premium",
     "average_rating": 4.4, "review_count": 211, "is_claimed": True,
     "short_description": "Late-night döner and lahmacun."},
]

SAMPLE_PHOTOS = [
    "https://images.unsplash.com/photo-1633321702518-7fecdafb94d5?w=800",
    "https://images.unsplash.com/photo-1601050690597-df0568f70950?w=800",
    "https://images.unsplash.com/photo-1561758033-d89a9ad46330?w=800",
]

# Enum-like view models for templates that use .value
class _StrEnum:
    def __init__(self, value): self.value = value
    def __str__(self): return self.value


class _UserVM:
    def __init__(self, id, email, display_name, role, verified=True, referred_by_user_id=None):
        self.id = id
        self.email = email
        self.display_name = display_name
        self.role = _StrEnum(role)
        self.email_verified = verified
        self.referred_by_user_id = referred_by_user_id


class _RestaurantVM:
    def __init__(self, **kw):
        self.id = kw["id"]; self.name = kw["name"]; self.slug = kw["slug"]
        self.address_line = kw.get("address_line", "123 Main St")
        self.city = kw.get("city", "Houston")
        self.state = kw.get("state", "TX")
        self.postal_code = kw.get("postal_code", "77002")
        self.country = "US"
        self.phone = kw.get("phone", "(713) 555-0100")
        self.website = kw.get("website", "https://example.com")
        self.description = kw.get("short_description", "")
        self.cuisine_tags = kw.get("cuisine_tags", [])
        self.price_range = _StrEnum(kw.get("price_range", "1"))
        self.tier = _StrEnum(kw.get("tier_value", "free"))
        self.halal_status = _StrEnum(kw.get("halal_status_value", "pending"))
        self.average_rating = kw.get("average_rating", 0)
        self.review_count = kw.get("review_count", 0)
        self.is_claimed = kw.get("is_claimed", False)
        self.is_active = True
        self.photos = [type("P", (), {"id": f"p{i}", "blob_url": u, "caption": f"Photo {i+1}"})()
                       for i, u in enumerate(SAMPLE_PHOTOS)]


SAMPLE_RESTAURANT_OBJS = [_RestaurantVM(**r) for r in SAMPLE_RESTAURANTS]


DINER = _UserVM(id="u1", email="ayesha@example.com", display_name="Ayesha", role="diner")
OWNER = _UserVM(id="u2", email="imran@karachikebab.com", display_name="Imran", role="restaurant_owner")
ADMIN = _UserVM(id="u3", email="admin@halalistic.app", display_name="Rashida (Admin)", role="platform_admin")


def common_ctx(user=None, **extra):
    ctx = dict(
        user=user, flash=None, app_version="0.11.0",
        settings=type("S", (), {
            "project_name": "Halalistic",
            "app_public_url": "https://halalistic.app",
            "stripe_publishable_key": "pk_test_demo",
            "points_values": {"min_redemption": 1000, "review": 50, "referral": 500, "signup_bonus": 100},
        })(),
    )
    # Fake request for templates that use request.url_for / request.base_url
    ctx["request"] = type("R", (), {
        "url_for": lambda *a, **k: "https://halalistic.app/share/deals/x",
        "base_url": "https://halalistic.app/",
    })()
    ctx.update(extra)
    return ctx


def render_all(env: Environment):
    pages = [
        # anon home
        ("home_anon", "home.html", common_ctx(
            user=None,
            featured=SAMPLE_RESTAURANT_OBJS[:3],
            deals=SAMPLE_DEALS,
        )),
        # diner home
        ("home_diner", "home.html", common_ctx(
            user=DINER,
            featured=SAMPLE_RESTAURANT_OBJS[:3],
            deals=SAMPLE_DEALS,
        )),
        # signup
        ("auth_signup", "auth_signup.html", common_ctx()),
        # login
        ("auth_login", "auth_login.html", common_ctx(user=None, error=None)),
        # password reset
        ("auth_password_reset", "auth_password_reset.html", common_ctx()),
        # restaurants list
        ("restaurants_list", "restaurants_list.html", common_ctx(
            user=DINER,
            restaurants=SAMPLE_RESTAURANT_OBJS,
            city="", cuisine="", q="", halal_only=True,
        )),
        # restaurant detail
        ("restaurant_detail", "restaurant_detail.html", common_ctx(
            user=DINER,
            restaurant=_RestaurantVM(**SAMPLE_RESTAURANTS[0]),
            reviews=[
                type("R", (), {"id": "rv1", "reviewer_id": "u10", "rating": 5, "body": "The seekh kebabs are out of this world. Service was warm and fast.", "created_at": date(2026, 7, 15), "tags": ["Family-friendly", "Generous portions"]})(),
                type("R", (), {"id": "rv2", "reviewer_id": "u11", "rating": 4, "body": "Solid Pakistani food in the loop. Parking can be rough at dinner.", "created_at": date(2026, 7, 8), "tags": ["Good for groups"]})(),
            ],
            reviewer_names={"u10": "Hassan K.", "u11": "Mariam T."},
            deals=[SAMPLE_DEALS[0]],
            certs=[type("C", (), {"id": "c1", "certifying_body": type("B", (), {"name": "ISNA"})(), "status": "approved", "issue_date": date(2026, 1, 15), "expiry_date": date(2027, 1, 15), "blob_url": ""})()],
            tags=["Family-friendly", "Generous portions", "Late-night", "Good for groups"],
        )),
        # deals list
        ("deals_list", "deals_list.html", common_ctx(
            user=DINER,
            deals=SAMPLE_DEALS,
            city="", deal_type="",
        )),
        # diner account
        ("account_dashboard", "account_dashboard.html", common_ctx(
            user=DINER,
            points=1450, referral_code="AYESHA-7K2",
            referral_link="https://halalistic.app/web/signup?ref=AYESHA-7K2",
            subscription=None,
            redemptions=[],
            points_min=1000,
        )),
        # owner dashboard
        ("owner_dashboard", "owner_dashboard.html", common_ctx(
            user=OWNER,
            restaurant=_RestaurantVM(**SAMPLE_RESTAURANTS[0]),
            active_deals=[SAMPLE_DEALS[0]],
            pending_deals=[],
            certs=[type("C", (), {"id": "c1", "certifying_body": type("B", (), {"name": "ISNA"})(), "status": "approved", "issue_date": date(2026, 1, 15), "expiry_date": date(2027, 1, 15), "blob_url": ""})()],
            photo_count=3,
        )),
        # owner edit
        ("owner_edit", "owner_edit.html", common_ctx(
            user=OWNER,
            restaurant=_RestaurantVM(**SAMPLE_RESTAURANTS[0]),
        )),
        # owner photos
        ("owner_photos", "owner_photos.html", common_ctx(
            user=OWNER,
            restaurant=_RestaurantVM(**SAMPLE_RESTAURANTS[0]),
            photos=[type("P", (), {"id": f"p{i}", "blob_url": u, "caption": f"Photo {i+1}"})()
                    for i, u in enumerate(SAMPLE_PHOTOS)],
        )),
        # owner certs
        ("owner_certs", "owner_certs.html", common_ctx(
            user=OWNER,
            restaurant=_RestaurantVM(**SAMPLE_RESTAURANTS[0]),
            certs=[type("C", (), {"id": "c1", "certifying_body": type("B", (), {"name": "ISNA"})(), "custom_certifying_body": None, "status": "approved", "issue_date": date(2026, 1, 15), "expiry_date": date(2027, 1, 15), "blob_url": ""})()],
            bodies=[type("B", (), {"id": "b1", "name": "ISNA", "country": "US"}), type("B", (), {"id": "b2", "name": "HMC", "country": "UK"})],
        )),
        # owner deals
        ("owner_deals", "owner_deals.html", common_ctx(
            user=OWNER,
            restaurant=_RestaurantVM(**SAMPLE_RESTAURANTS[0]),
            deals=[type("D", (), {**SAMPLE_DEALS[0], "start_date": date.today(), "status": "approved", "target_audience": "public", "discount_value": "20"})()],
            deal_types=[_StrEnum("percentage_off"), _StrEnum("fixed_amount"), _StrEnum("bogo"), _StrEnum("free_item"), _StrEnum("bundle")],
            audiences=[_StrEnum("public"), _StrEnum("diner_only"), _StrEnum("push_only")],
        )),
    ]
    for name, tpl, ctx in pages:
        html = env.get_template(tpl).render(**ctx)
        (OUT / f"{name}.html").write_text(html, encoding="utf-8")
        print(f"  rendered {name}.html  ({len(html)/1024:.1f} KB)")
    return [name for name, _, _ in pages]


async def screenshot_all(pages):
    """Use Playwright via the MCP to capture mobile + desktop for each page."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for name in pages:
            url = f"file://{(OUT / (name + '.html')).resolve()}"
            for view in ("desktop", "mobile"):
                ctx = await browser.new_context(
                    viewport={"width": 1280, "height": 900} if view == "desktop" else {"width": 390, "height": 844},
                    device_scale_factor=2,
                )
                page = await ctx.new_page()
                await page.goto(url, wait_until="networkidle", timeout=20000)
                # Wait for Tailwind Play CDN to apply styles
                await page.wait_for_timeout(1500)
                out = SHOTS / f"{name}__{view}.png"
                await page.screenshot(path=str(out), full_page=True)
                await ctx.close()
                size = out.stat().st_size / 1024
                print(f"  shot  {out.name}  ({size:.1f} KB)")
        await browser.close()


def main() -> int:
    env = build_env()
    print("Rendering templates...")
    pages = render_all(env)
    print(f"Done. {len(pages)} pages in {OUT}")
    print("Screenshots...")
    asyncio.run(screenshot_all(pages))
    print(f"Done. Shots in {SHOTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
