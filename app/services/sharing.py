"""Sharing service — build canonical share URLs + Open Graph meta page (Stage 9).

Per BRD §3.7: a diner shares a restaurant or deal with friends. The
backend's job is to produce a clean deep-link URL plus an HTML page
with Open Graph meta tags so Twitter/Slack/iMessage/Discord render a
good preview card. The frontend then calls `navigator.share({url, ...})`
or just copies the URL to clipboard.

URL shape (intentionally short and stable):
  {APP_PUBLIC_URL}/share/deals/{deal_id}
  {APP_PUBLIC_URL}/share/restaurants/{restaurant_id}

The HTML page at that URL is a 0-JS redirect-style page with:
  - <title>, <meta name="description">
  - <meta property="og:title">, og:description, og:image, og:url, og:type
  - <meta name="twitter:card" = "summary_large_image">
  - <link rel="canonical" ...>
  - A noscript body with a clickable link (so unfurlers that don't
    execute meta can still find the target URL)
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urljoin

from app.core.config import settings
from app.models.deal import Deal
from app.models.restaurant import Restaurant

logger = logging.getLogger("halalistic.sharing")


def deal_share_url(deal_id) -> str:
    return f"{settings.app_public_url.rstrip('/')}/share/deals/{deal_id}"


def restaurant_share_url(restaurant_id) -> str:
    return f"{settings.app_public_url.rstrip('/')}/share/restaurants/{restaurant_id}"


def deal_card_image_url(deal_id) -> str:
    return f"{settings.app_public_url.rstrip('/')}/share/deals/{deal_id}/card.png"


def restaurant_card_image_url(restaurant_id) -> str:
    return f"{settings.app_public_url.rstrip('/')}/share/restaurants/{restaurant_id}/card.png"


def build_deal_og_html(deal: Deal, restaurant: Restaurant) -> str:
    """Return a tiny HTML page with full OG / Twitter meta tags so that
    unfurlers (Slack, Twitter, iMessage, WhatsApp) render a good
    preview. The visible body is a clickable fallback.
    """
    title = f"{deal.title} — {restaurant.name}"
    description = (deal.description
                   or f"New deal at {restaurant.name}. Halalistic.")
    image_url = deal_card_image_url(deal.id)
    page_url = deal_share_url(deal.id)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_e(title)}</title>
<meta name="description" content="{_e(description)}">
<link rel="canonical" href="{_e(page_url)}">
<meta property="og:type" content="website">
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(description)}">
<meta property="og:url" content="{_e(page_url)}">
<meta property="og:image" content="{_e(image_url)}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_e(title)}">
<meta name="twitter:description" content="{_e(description)}">
<meta name="twitter:image" content="{_e(image_url)}">
<meta http-equiv="refresh" content="0; url={_e(page_url)}">
</head>
<body>
<p>Redirecting to <a href="{_e(page_url)}">{_e(title)}</a>…</p>
</body>
</html>"""


def build_restaurant_og_html(restaurant: Restaurant) -> str:
    title = f"{restaurant.name} — Halalistic"
    description = (restaurant.description
                   or f"{restaurant.name} · {restaurant.city} · {restaurant.address_line}")
    image_url = restaurant_card_image_url(restaurant.id)
    page_url = restaurant_share_url(restaurant.id)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_e(title)}</title>
<meta name="description" content="{_e(description)}">
<link rel="canonical" href="{_e(page_url)}">
<meta property="og:type" content="website">
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(description)}">
<meta property="og:url" content="{_e(page_url)}">
<meta property="og:image" content="{_e(image_url)}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_e(title)}">
<meta name="twitter:description" content="{_e(description)}">
<meta name="twitter:image" content="{_e(image_url)}">
<meta http-equiv="refresh" content="0; url={_e(page_url)}">
</head>
<body>
<p>Redirecting to <a href="{_e(page_url)}">{_e(title)}</a>…</p>
</body>
</html>"""


def _e(s: str) -> str:
    """HTML-escape the bare minimum. The values come from admin-owned
    DB rows (deal title, restaurant name) so full sanitization is
    overkill, but & < > " ' still need to be escaped."""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&#39;"))
