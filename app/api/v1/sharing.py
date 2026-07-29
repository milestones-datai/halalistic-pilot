"""Sharing + deal-card API (Stage 9).

Endpoints:
  GET /share/deals/{deal_id}            → HTML page with full OG meta tags
  GET /share/deals/{deal_id}/card.png   → 1080x1920 PNG (Instagram Stories)
  GET /share/deals/{deal_id}/card-og.png → 1200x630 PNG (OG / link preview)
  GET /share/restaurants/{rid}          → HTML page with full OG meta tags
  GET /share/restaurants/{rid}/card.png  → 1080x1920 PNG

The HTML page is a 0-JS meta-redirect + noscript fallback so social
unfurlers (Slack, Twitter, iMessage, etc.) that don't execute JS still
see the right preview.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.deal import Deal
from app.models.restaurant import Restaurant
from app.services.deal_cards import render_deal_card
from app.services.sharing import (
    build_deal_og_html,
    build_restaurant_og_html,
)

router = APIRouter(prefix="/share", tags=["share"])


# ----- Deals -----
@router.get("/deals/{deal_id}", response_class=HTMLResponse)
async def share_deal_page(
    deal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    deal = await db.get(Deal, deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="deal not found")
    restaurant = await db.get(Restaurant, deal.restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail="restaurant not found")
    return HTMLResponse(content=build_deal_og_html(deal, restaurant))


@router.get("/deals/{deal_id}/card.png")
async def share_deal_card_story(
    deal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Response:
    deal = await db.get(Deal, deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="deal not found")
    restaurant = await db.get(Restaurant, deal.restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail="restaurant not found")
    png = render_deal_card(deal, restaurant, size="story")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/deals/{deal_id}/card-og.png")
async def share_deal_card_og(
    deal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Response:
    deal = await db.get(Deal, deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="deal not found")
    restaurant = await db.get(Restaurant, deal.restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail="restaurant not found")
    png = render_deal_card(deal, restaurant, size="og")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


# ----- Restaurants -----
@router.get("/restaurants/{restaurant_id}", response_class=HTMLResponse)
async def share_restaurant_page(
    restaurant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    restaurant = await db.get(Restaurant, restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail="restaurant not found")
    return HTMLResponse(content=build_restaurant_og_html(restaurant))


@router.get("/restaurants/{restaurant_id}/card.png")
async def share_restaurant_card_story(
    restaurant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Response:
    restaurant = await db.get(Restaurant, restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail="restaurant not found")
    png = render_deal_card(deal=None, restaurant=restaurant, size="story")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})
