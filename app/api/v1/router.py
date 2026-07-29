"""Aggregator for v1 API routes."""
from fastapi import APIRouter

from app.api.v1 import (
    admin,
    auth,
    billing,
    deals,
    halal,
    menu,
    points,
    push,
    restaurants,
    reviews,
    sharing,
    tags,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(halal.router)
api_router.include_router(restaurants.router)
api_router.include_router(restaurants.search_router)
api_router.include_router(menu.router)
api_router.include_router(reviews.router)
api_router.include_router(tags.router)
api_router.include_router(deals.router)
api_router.include_router(deals.admin_router)
api_router.include_router(billing.restaurant_billing_router)
api_router.include_router(billing.user_billing_router)
api_router.include_router(billing.webhook_router)
api_router.include_router(points.router)
api_router.include_router(points.admin_router)
api_router.include_router(sharing.router)
api_router.include_router(push.router)
