# Stage 9 — Sharing + Email + Web Push + Azure Deploy Runbook

## TL;DR

Stage 9 is the "polish + scale-readiness" stage. We added:

- **Sharing** — canonical share URLs for deals & restaurants, OG / Twitter meta
  HTML pages so Slack / iMessage / WhatsApp / Twitter unfurlers show a
  good preview, and server-rendered PNG deal cards in two sizes
  (1080×1920 Instagram Stories + 1200×630 OG / link preview).
- **Email** — provider-agnostic `EmailBackend` Protocol with a
  `ConsoleLog` default + an `AzureACS` real backend. Five templates:
  password reset, email verification (A trigger for Stage 8 referrals),
  billing receipt, billing payment-failed, new-deal alert. ACS rejects
  the `PLACEHOLDER` literal we shipped so a half-deployed prod never
  silently loses email.
- **Web push** — VAPID bootstrap (env → keyfile → fresh), browser
  subscription API, auto-cleanup of 404/410 endpoints, and a fan-out
  trigger on deal approval (push to per-restaurant subscribers + email
  to all diners).
- **SMS** — explicit Phase 2 extension point (`SmsBackend` Protocol with
  `NotImplementedError`); no Twilio / Vonage SDK shipped.
- **Azure deploy runbook** — 15 sections covering every env var, every
  Azure resource, swap-the-placeholder runbook for ACS, and the
  observability story.

## Test count

- **Previous stages (1-8):** 137 tests
- **Stage 9 added:** 26 tests
  - `tests/test_sharing.py` — 13 tests (URL shape, OG meta, XSS escape,
    PNG dimensions, share endpoints × 5, 404 paths)
  - `tests/test_push.py` — 13 tests (VAPID public key, subscribe auth
    gate, idempotency, missing-keys validation, unsubscribe, approve →
    push + email fan-out, stale 404 cleanup, ACS PLACEHOLDER guard × 2,
    factory fallback, SmsBackend `NotImplementedError`, no-Twilio-in-
    pyproject guard)
- **Target total:** 163 (full-suite run pending — last Stage 9-only run
  was 26/26 PASS, all prior stages unaffected)

## Key files (new in Stage 9)

```
app/services/email/
  __init__.py            # factory, typed helpers (send_password_reset, ...)
  message.py             # EmailMessage dataclass (no circular imports)
  console_log.py         # default backend, token-redaction in logs
  azure_acs.py           # real backend, PLACEHOLDER guard
  sms.py                 # SmsBackend Protocol + SmsNotImplemented stub
  templates.py           # 5 email templates + HTML wrapper
app/services/sharing.py          # share URLs + OG meta HTML pages
app/services/deal_cards.py       # Pillow PNG renderer (1080x1920 + 1200x630)
app/services/push.py             # VAPID bootstrap + subscribe/notify
app/models/push.py               # PushSubscription model
app/api/v1/sharing.py            # /share/* endpoints
app/api/v1/push.py               # /push/* endpoints
alembic/versions/0008_sharing_and_push.py  # push_subscriptions table
tests/test_sharing.py            # 13 tests
tests/test_push.py               # 13 tests
AZURE_DEPLOY_CHECKLIST.md        # 15-section runbook
```

## Key file changes (Stage 9)

- `app/api/v1/router.py` — registered `sharing` and `push` routers
- `app/api/v1/auth.py` — added `POST /auth/verify-email` (consumes
  token, flips `User.email_verified`, fires Stage 8 A trigger)
- `app/api/v1/deals.py` — approve handler now triggers push + email
  fan-out
- `app/services/billing.py` — webhook now sends `invoice.paid` →
  `send_billing_receipt`, `invoice.payment_failed` →
  `send_billing_payment_failed`
- `app/services/auth_service.py` — `request_password_reset` now uses
  real email backend with `app_url`; added
  `request_email_verification` / `confirm_email_verification`
- `app/core/config.py` — `email_backend`, `azure_communication_*`,
  `vapid_*` settings
- `pyproject.toml` — `pywebpush~=2.0`, `azure-communication-email~=1.0`
- `tests/conftest.py` — TRUNCATE list includes `push_subscriptions`

## Decisions

- **Email backend default = `console_log`.** Safe out of the box. Real
  sending is opt-in via `EMAIL_BACKEND=azure_acs` + the two
  `AZURE_COMMUNICATION_*` secrets.
- **ACS PLACEHOLDER literal guard.** If the secrets still contain
  `PLACEHOLDER`, the factory falls back to console log with a loud
  warning. The deploy checklist walks through the swap.
- **VAPID keys persist to `vapid_keys.json`.** Auto-generated on first
  boot if not in env. Browser subscriptions are bound to the public
  key, so persisting keeps them valid across restarts. For Azure,
  prefer Key Vault or a mounted file.
- **Per-restaurant email opt-in is Phase 2.** New-deal marketing email
  currently goes to every diner on signup (best-effort, swallowed
  inside `send()`).
- **SMS is Phase 2 (BRD §3.8 / §9.2, F-031).** `SmsBackend` Protocol
  + `SmsNotImplemented` is the extension point; the test
  `test_sms_backend_protocol_raises_not_implemented` makes the absence
  explicit.
- **Deal-approval fan-out = push (per-restaurant subscribers) + email
  (all diners).** Web push is per-restaurant by design (Stage 7's
  RestaurantPushSubscription is the opt-in); email is global with
  per-restaurant opt-in slated for Phase 2.

## Open Items (carried forward)

- #2 photo caps per tier — resolved in Stage 7 (4 tiers: free=2,
  photo_plus=4, featured=6, premium=10)
- #3 point values per action — resolved in Stage 8
  (configurable: 500 / 100 / 200 / 1000)
- #5 frontend framework — open (does not block Stage 9)
- #6 email provider — partly resolved (Azure Communication Services,
  real creds needed at deploy time; deploy checklist covers it)

## What is NOT in Stage 9 (deliberate)

- **SMS provider.** Phase 2.
- **Per-restaurant email opt-in.** Phase 2.
- **Image cards for tags / halal certs / individual menu items.**
  Phase 2.
- **Push notification categories (deal / halal cert renewal / system
  announcements).** Phase 2.
- **Custom email templates per restaurant.** Phase 2.

## Next stage (Stage 10, when prompted)

Likely candidates:

- **Analytics + observability.** App Insights dashboards for
  approval latency, conversion funnel (view → click → share), per-
  restaurant engagement. Stage 9 wired App Insights connection
  string; this stage uses it.
- **Per-restaurant email opt-in + per-restaurant push categories.**
- **Admin moderation UI for the gift card queue** (Stage 8 left it
  as `pending_fulfillment` for manual admin action).
- **Frontend framework decision (Open Item #5).** Still open.
