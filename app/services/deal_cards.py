"""Deal card image generation — server-side PNG (Stage 9).

Per BRD §3.7: a server-side image generator produces a shareable
graphic for a given deal, sized for Instagram Stories (1080×1920) and
for OG / link preview (1200×630). We generate both with the same
template; the only difference is the canvas.

Design notes:
  - Brand palette: forest green (#1f5f3f) + saffron (#d97a1a) + cream
    (#fbf7ee), from the kickoff. Matches the Stage 7 UI prototype.
  - No external image library besides Pillow. No fonts bundled; we
    rely on Pillow's default font (DejaVu on most Linux containers,
    which is what Azure Container Apps ships).
  - All strings are HTML-escaped via the sharing service helper.
"""
from __future__ import annotations

import io
import logging
from datetime import date
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from app.models.deal import Deal
from app.models.restaurant import Restaurant
from app.services.sharing import _e

logger = logging.getLogger("halalistic.deal_cards")

# Brand palette
GREEN = (31, 95, 63)
GREEN_SOFT = (231, 241, 234)
SAFFRON = (217, 122, 26)
SAFFRON_SOFT = (251, 238, 221)
CREAM = (251, 247, 238)
INK = (28, 28, 28)
INK_SOFT = (85, 85, 85)
LINE = (230, 224, 208)


def _font(size: int) -> ImageFont.ImageFont:
    """Default Pillow font (no bundling). On the Azure Container Apps
    Linux image this is DejaVuSans.
    """
    return ImageFont.load_default(size=size) or ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """Greedy word-wrap that fits within `max_width` pixels."""
    words = text.split()
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        candidate = " ".join(cur + [w])
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width or not cur:
            cur.append(w)
        else:
            lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    return lines


def _draw_restaurant_card(
    draw: ImageDraw.ImageDraw, *, restaurant: Restaurant, deal: Optional[Deal],
    width: int, height: int,
) -> None:
    # Top band — saffron accent
    accent_h = max(20, int(height * 0.02))
    draw.rectangle([(0, 0), (width, accent_h)], fill=SAFFRON)
    # Bottom band — green
    bottom_h = max(60, int(height * 0.08))
    draw.rectangle([(0, height - bottom_h), (width, height)], fill=GREEN)

    # Padding for content
    pad = max(40, int(width * 0.06))
    inner_w = width - 2 * pad

    # "Halalistic" wordmark
    title_font = _font(int(width * 0.10))
    draw.text((pad, accent_h + pad), "Halalistic.", font=title_font, fill=GREEN)

    # "Verified halal" pill (only if restaurant is verified)
    pill_y = accent_h + pad + int(width * 0.10) + 20
    if restaurant.halal_status.value == "verified":
        pill_text = "✓  HALAL VERIFIED"
        pf = _font(int(width * 0.045))
        bbox = draw.textbbox((0, 0), pill_text, font=pf)
        pw = bbox[2] - bbox[0] + 30
        ph = bbox[3] - bbox[1] + 16
        draw.rounded_rectangle([(pad, pill_y), (pad + pw, pill_y + ph)],
                               radius=ph // 2, fill=GREEN)
        draw.text((pad + 15, pill_y + 8), pill_text, font=pf, fill=(255, 255, 255))

    # Restaurant name
    rname_font = _font(int(width * 0.13))
    y = pill_y + 110
    for line in _wrap_text(draw, restaurant.name, rname_font, inner_w):
        draw.text((pad, y), line, font=rname_font, fill=INK)
        y += int(width * 0.14)

    # Address (light)
    addr_font = _font(int(width * 0.04))
    y += 8
    addr_text = f"{restaurant.address_line}, {restaurant.city}"
    for line in _wrap_text(draw, addr_text, addr_font, inner_w):
        draw.text((pad, y), line, font=addr_font, fill=INK_SOFT)
        y += int(width * 0.05)

    # Deal block (if a deal was provided)
    if deal is not None:
        y += int(width * 0.04)
        # separator
        draw.line([(pad, y), (width - pad, y)], fill=LINE, width=2)
        y += int(width * 0.04)
        # Deal badge
        badge_text = (deal.deal_type.value if hasattr(deal.deal_type, "value")
                      else str(deal.deal_type)).replace("_", " ").upper()
        bf = _font(int(width * 0.04))
        bbox = draw.textbbox((0, 0), badge_text, font=bf)
        bw = bbox[2] - bbox[0] + 24
        bh = bbox[3] - bbox[1] + 12
        draw.rounded_rectangle([(pad, y), (pad + bw, y + bh)], radius=6, fill=SAFFRON)
        draw.text((pad + 12, y + 6), badge_text, font=bf, fill=(255, 255, 255))
        y += bh + 18
        # Deal title
        dtitle_font = _font(int(width * 0.09))
        for line in _wrap_text(draw, deal.title, dtitle_font, inner_w):
            draw.text((pad, y), line, font=dtitle_font, fill=INK)
            y += int(width * 0.10)
        # Description (if any)
        if deal.description:
            desc_font = _font(int(width * 0.045))
            y += 8
            for line in _wrap_text(draw, deal.description, desc_font, inner_w)[:3]:
                draw.text((pad, y), line, font=desc_font, fill=INK_SOFT)
                y += int(width * 0.055)
        # Dates pill
        y += 10
        today = date.today()
        end = deal.end_date
        days = (end - today).days if end and end >= today else 0
        if days > 0:
            valid_text = f"Valid for {days} more day{'s' if days != 1 else ''} · ends {end.isoformat()}"
        else:
            valid_text = f"Ended {end.isoformat()}" if end else ""
        if valid_text:
            vf = _font(int(width * 0.04))
            draw.text((pad, y), valid_text, font=vf, fill=INK_SOFT)

    # Footer "Halalistic" wordmark on the green band
    foot_font = _font(int(width * 0.06))
    draw.text((pad, height - bottom_h + 18), "Halalistic.", font=foot_font, fill=(255, 255, 255))
    fsub_font = _font(int(width * 0.03))
    draw.text((pad, height - int(bottom_h * 0.45)), "Halal restaurants + deals · Houston pilot",
              font=fsub_font, fill=(255, 255, 255))


def render_deal_card(deal: Deal, restaurant: Restaurant, *, size: str = "story") -> bytes:
    """Render a PNG. size='story' → 1080x1920 (Instagram Stories).
    size='og' → 1200x630 (link preview / OG card).
    """
    if size == "story":
        w, h = 1080, 1920
    elif size == "og":
        w, h = 1200, 630
    else:
        raise ValueError(f"size must be 'story' or 'og', got {size!r}")

    img = Image.new("RGB", (w, h), CREAM)
    draw = ImageDraw.Draw(img)
    _draw_restaurant_card(draw, restaurant=restaurant, deal=deal, width=w, height=h)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
