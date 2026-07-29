"""Web (consumer + owner portal) package — Stage 11.

Server-rendered Jinja2 + a touch of HTMX, sharing the same auth
infrastructure (signed-cookie session via SessionMiddleware) as the
admin console. The split is purely URL-level:
  /                  consumer home / auth
  /restaurants/...    consumer discovery + profiles + reviews + deals
  /account/...       diner account (points, referrals, gift cards, subscription)
  /owner/...         restaurant-owner portal (CRUD their own data)

A diner who lands on /owner/* gets 403 with a clear "owner only"
message rendered as an HTML page. An owner who lands on /admin/ui/*
gets the admin UI's existing 403 (admin-only).
"""
