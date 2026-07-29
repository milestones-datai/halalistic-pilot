# Halalistic — Azure Deployment Checklist (Stage 9)

Pilot target: Houston, single-region Azure Container Apps + PostgreSQL Flexible
Server + Azure Blob Storage + Azure Communication Services (email) + Stripe
webhook. This file is the runbook for going from `halalistic-stage9.zip` to
a live URL behind `app.halalistic.com`.

> **Status of the code in this stage (intentional):** the email backend
> defaults to `console_log` and the VAPID keys are auto-generated on first
> boot. Real Azure resources (ACS, VAPID) are **not** required to ship
> the app; you can pilot with the in-memory equivalents and swap them in
> later. Each section below tells you which env vars unlock which feature.

---

## 0. Pre-flight (5 min)

- [ ] Azure subscription created, logged in via `az login`
- [ ] `az account set --subscription <SUB_ID>`
- [ ] `az provider register -n Microsoft.App --wait`
- [ ] `az provider register -n Microsoft.OperationalInsights --wait`
- [ ] `az provider register -n Microsoft.DocumentDB --wait` (only if you
  later swap PG for Cosmos)
- [ ] Stripe account created (test mode first), live keys ready
- [ ] Stripe products + prices created per the IDs in `core/config.py`
- [ ] VAPID keypair generated for web push (one-time, per environment)

```bash
# Generate VAPID keys (one-time per environment; reuse the same pair
# across restarts so existing browser subscriptions stay valid).
python -c "import py_vapid; print(py_vapid.Vapid().private_pem())" \
  > halalistic-vapid-private.pem
python -c "import py_vapid; print(py_vapid.Vapid().public_key_urlsafe_base64())" \
  > halalistic-vapid-public.txt
```

---

## 1. Resource Group + Identity (10 min)

```bash
RG=halalistic-pilot
LOC=centralus
az group create -n $RG -l $LOC

# Container Apps needs a managed identity for ACR pull + Key Vault
az identity create -g $RG -n halalistic-app-identity
MI_PRINCIPAL=$(az identity show -g $RG -n halalistic-app-identity --query principalId -o tsv)
MI_CLIENT=$(az identity show -g $RG -n halalistic-app-identity --query clientId -o tsv)
```

## 2. PostgreSQL Flexible Server (15 min)

```bash
DB_PWD=$(openssl rand -base64 32)
az postgres flexible-server create \
  -g $RG -n halalistic-pg \
  -l $LOC --tier Burstable --sku-name Standard_B1ms \
  --storage-size 32 --version 16 \
  --admin-user halalistic --admin-password "$DB_PWD"

az postgres flexible-server db create \
  -g $RG -s halalistic-pg -d halalistic

# Network: allow Azure services (Container Apps will use VNet integration)
# For a tight pilot setup, allow public access from the VNet egress IP.
az postgres flexible-server firewall-rule create \
  -g $RG -s halalistic-pg -n allow-container-apps \
  --start-ip-address 0.0.0.0 --end-ip-address 255.255.255.255
```

> **Secret hygiene:** write the admin password into Key Vault, do not
> paste it into a `.env`. See §6.

## 3. Storage Account (5 min) — photo uploads

```bash
az storage account create -g $RG -n halalisticstg -l $LOC \
  --sku Standard_LRS --kind StorageV2

CONN=$(az storage account show-connection-string -g $RG -n halalisticstg -o tsv)
az storage container create -n photos --connection-string "$CONN" --public-access off
```

## 4. Container Registry (5 min)

```bash
az acr create -g $RG -n halalisticacr --sku Basic --admin-enabled true
az role assignment create --assignee $MI_PRINCIPAL \
  --role AcrPull --scope $(az acr show -g $RG -n halalisticacr --query id -o tsv)
```

## 5. Log Analytics + Application Insights (5 min)

```bash
az monitor log-analytics workspace create -g $RG -n halalistic-logs -l $LOC
LAWS_ID=$(az monitor log-analytics workspace show -g $RG -n halalistic-logs --query customerId -o tsv)
LAWS_KEY=$(az monitor log-analytics workspace get-shared-keys -g $RG -n halalistic-logs --query primarySharedKey -o tsv)
az monitor app-insights component create -g $RG -a halalistic-ai -l $LOC \
  --workspace $LAWS_ID
APPINSIGHTS_CONN=$(az monitor app-insights component show -g $RG -a halalistic-ai --query connectionString -o tsv)
```

## 6. Key Vault (5 min) — secret store

```bash
az keyvault create -g $RG -n halalistic-kv -l $LOC \
  --enable-rbac-authorization true

# Grant the app identity read access
az role assignment create --assignee $MI_PRINCIPAL \
  --role "Key Vault Secrets User" \
  --scope $(az keyvault show -g $RG -n halalistic-kv --query id -o tsv)
```

Push all secrets to Key Vault. After this, **never** put a secret in the
container app env directly — read from KV at boot via the env-var
`@Microsoft.KeyVault(SecretUri=...)` syntax below.

```bash
KV=halalistic-kv
az keyvault secret set --vault-name $KV --name DATABASE-URL        --value "postgresql+asyncpg://halalistic:$DB_PWD@halalistic-pg.postgres.database.azure.com/halalistic?sslmode=require"
az keyvault secret set --vault-name $KV --name SECRET-KEY          --value "$(openssl rand -base64 64)"
az keyvault secret set --vault-name $KV --name JWT-AUD             --value "halalistic-api"
az keyvault secret set --vault-name $KV --name JWT-ISS             --value "https://app.halalistic.com"
az keyvault secret set --vault-name $KV --name STORAGE-CONNECTION --value "$CONN"
az keyvault secret set --vault-name $KV --name STRIPE-SECRET-KEY   --value "sk_live_..."
az keyvault secret set --vault-name $KV --name STRIPE-WEBHOOK-SECRET --value "whsec_..."
az keyvault secret set --vault-name $KV --name GOOGLE-MAPS-KEY     --value "AIza..."
az keyvault secret set --vault-name $KV --name VAPID-PRIVATE-KEY   --value "$(cat halalistic-vapid-private.pem)"
az keyvault secret set --vault-name $KV --name AZURE-COMMUNICATION-CONNECTION-STRING --value "PLACEHOLDER_REPLACE_AT_DEPLOY"
az keyvault secret set --vault-name $KV --name AZURE-COMMUNICATION-SENDER-ADDRESS   --value "PLACEHOLDER_REPLACE_AT_DEPLOY"
```

> **The two `PLACEHOLDER_REPLACE_AT_DEPLOY` secrets are intentional.**
> The Stage 9 ACS backend hard-rejects any value containing the literal
> `PLACEHOLDER` and falls back to console-log. This prevents a
> half-deployed app from silently losing email. Once ACS is fully
> provisioned (§9), `az keyvault secret set` again with the real
> values and **re-start** the Container App revision to pick them up.

## 7. Build + Push Image (10 min)

```bash
cd halalistic
az acr build --registry halalisticacr --image halalistic-api:stage9 .
```

## 8. Container Apps Environment + App (10 min)

```bash
az containerapp env create -g $RG -n halalistic-env -l $LOC \
  --logs-workspace-id $LAWS_ID --logs-workspace-key $LAWS_KEY

az containerapp create -g $RG -n halalistic-api \
  --environment halalistic-env \
  --image halalisticacr.azurecr.io/halalistic-api:stage9 \
  --registry-server halalisticacr.azurecr.io \
  --registry-identity $(az identity show -g $RG -n halalistic-app-identity --query id -o tsv) \
  --user-assigned-identity $MI_CLIENT \
  --ingress external --target-port 8000 \
  --min-replicas 1 --max-replicas 3 \
  --cpu 1.0 --memory 2.0Gi \
  --env-vars \
    ENV=production \
    APP-PUBLIC-URL=https://app.halalistic.com \
    APPINSIGHTS-CONNECTION-STRING="$APPINSIGHTS_CONN" \
    AZURE-KEYVAULT-URI=https://halalistic-kv.vault.azure.net/ \
    DATABASE-URL="secretref:DATABASE-URL" \
    SECRET-KEY="secretref:SECRET-KEY" \
    JWT-AUD="secretref:JWT-AUD" \
    JWT-ISS="secretref:JWT-ISS" \
    STORAGE-CONNECTION-STRING="secretref:STORAGE-CONNECTION" \
    STRIPE-SECRET-KEY="secretref:STRIPE-SECRET-KEY" \
    STRIPE-WEBHOOK-SECRET="secretref:STRIPE-WEBHOOK-SECRET" \
    GOOGLE-MAPS-API-KEY="secretref:GOOGLE-MAPS-KEY" \
    VAPID-PRIVATE-KEY="secretref:VAPID-PRIVATE-KEY" \
    VAPID-CLAIMS-EMAIL="ops@halalistic.com" \
    EMAIL-BACKEND=azure_acs \
    AZURE-COMMUNICATION-CONNECTION-STRING="secretref:AZURE-COMMUNICATION-CONNECTION-STRING" \
    AZURE-COMMUNICATION-SENDER-ADDRESS="secretref:AZURE-COMMUNICATION-SENDER-ADDRESS" \
    TIER-STRIPE-PRICE-FREE="" \
    TIER-STRIPE-PRICE-PHOTO-PLUS="price_..." \
    TIER-STRIPE-PRICE-FEATURED="price_..." \
    TIER-STRIPE-PRICE-PREMIUM="price_..." \
    POINTS-REFERRAL=500 \
    POINTS-REVIEW=100 \
    POINTS-CHECKIN=200 \
    POINTS-MIN-REDEMPTION=1000
```

> The `secretref:` prefix tells the ACA runtime to resolve the value
> from Key Vault via the managed identity. No secret ever lives in
> the portal UI.

## 9. Azure Communication Services — Email (15 min) — *optional at go-live*

If you want real transactional email on day one, do this before opening
signup:

```bash
az communication create -g $RG -n halalistic-acs --location global \
  --data-location UnitedStates

ACS_CONN=$(az communication list-key -g $RG -n halalistic-acs --query primaryConnectionString -o tsv)

# Provision a sender domain. The recommended path is to link your
# real domain (e.g. mail.halalistic.com) and add the SPF/DKIM records
# Azure gives you to your DNS.
az communication email domain create -g $RG -n halalistic-acs \
  --domain-name mail.halalistic.com

# Verify the domain ownership + DKIM via the email Azure sends to
# the WHOIS/RDAP-listed registrant. After verification, set the
# sender address to something like "Halalistic <noreply@halalistic.com>".

# Swap the placeholder secrets with the real ones
az keyvault secret set --vault-name halalistic-kv \
  --name AZURE-COMMUNICATION-CONNECTION-STRING --value "$ACS_CONN"
az keyvault secret set --vault-name halalistic-kv \
  --name AZURE-COMMUNICATION-SENDER-ADDRESS \
  --value "Halalistic <noreply@halalistic.com>"

# Force a revision restart so the app picks up the new secrets
az containerapp revision restart -g $RG -n halalistic-api --revision $(az containerapp revision list -g $RG -n halalistic-api --query "[0].name" -o tsv)
```

> **If you skip §9 at go-live:** the app will still boot and the
> `EMAIL-BACKEND` will silently fall back to `console_log`. Password
> reset, email verification, billing receipts, and new-deal alerts
> will be printed to stdout instead of emailed. Suitable for
> internal testing; **not** suitable for a public pilot.

## 10. Custom domain + TLS (10 min)

```bash
az containerapp hostname add -g $RG -n halalistic-api --hostname app.halalistic.com
az containerapp hostname bind -g $RG -n halalistic-api --hostname app.halalistic.com \
  --environment halalistic-env \
  --validation-method CNAME
```

Add the CNAME the command prints to your DNS provider.

## 11. Stripe webhook (5 min)

```bash
# In the Stripe dashboard (or via API):
# URL:    https://app.halalistic.com/api/v1/billing/stripe/webhook
# Events: checkout.session.completed
#         customer.subscription.{created,updated,deleted}
#         invoice.paid
#         invoice.payment_failed
#
# Then set STRIPE-WEBHOOK-SECRET to the whsec_... value Stripe shows
# you and update Key Vault:
az keyvault secret set --vault-name halalistic-kv --name STRIPE-WEBHOOK-SECRET --value "whsec_..."
az containerapp revision restart ...
```

## 12. CORS + custom domain final (5 min)

The API does **not** set CORS allow_origins in env (per Stage 1 base).
Set via env on the Container App:

```bash
az containerapp update -g $RG -n halalistic-api \
  --set-env-vars CORS-ALLOW-ORIGINS="https://app.halalistic.com"
```

## 13. Database migrate + seed (5 min)

```bash
# From a local machine with VPN access OR from a one-off container:
DATABASE_URL="postgresql+asyncpg://halalistic:$DB_PWD@halalistic-pg.postgres.database.azure.com/halalistic?sslmode=require" \
  alembic upgrade head
DATABASE_URL="..." python scripts/seed.py
```

## 14. Smoke tests (10 min)

```bash
curl -f https://app.halalistic.com/api/v1/health
# Stage 2 readiness: register a user, verify email, get a token, hit /me.
```

The Stage 9 `run-dod.ps1` is **dev-box** only (it uses `docker compose`).
For Azure, run a 1-shot container in the same vnet and curl the public
endpoint instead.

## 15. Observability

- Application Insights connection string is wired via
  `APPINSIGHTS-CONNECTION-STRING`; the `app/main.py` startup
  initialises the OpenTelemetry exporter.
- Log Analytics workspace receives Container App stdout/stderr.
- Health endpoint: `GET /api/v1/health` returns
  `{ "db": "ok" | "down", "stripe_configured": bool, "vapid_configured": bool }`.

---

## Env-var matrix — single source of truth

| Env var | Where it lives | Used by | What happens if missing/placeholder |
| --- | --- | --- | --- |
| `DATABASE_URL` | Key Vault | SQLAlchemy engine | App refuses to start |
| `SECRET_KEY` | Key Vault | JWT signing | App refuses to start |
| `JWT_AUD` | Key Vault | JWT validation | Tokens rejected |
| `JWT_ISS` | Key Vault | JWT validation | Tokens rejected |
| `STORAGE_CONNECTION_STRING` | Key Vault | Azure Blob uploads | Photo upload returns 503 |
| `STRIPE_SECRET_KEY` | Key Vault | Stripe SDK | Checkout/webhook return 503 |
| `STRIPE_WEBHOOK_SECRET` | Key Vault | Webhook signature | Webhook returns 400 |
| `GOOGLE_MAPS_API_KEY` | Key Vault | Geocoding | Restaurant create returns 503 |
| `VAPID_PRIVATE_KEY` | Key Vault | Web push | Push subscribe returns 503 |
| `VAPID_CLAIMS_EMAIL` | Container App env | Web push | Push subscribe returns 503 |
| `EMAIL_BACKEND` | Container App env | Email factory | Defaults to `console_log` |
| `AZURE_COMMUNICATION_CONNECTION_STRING` | Key Vault | ACS backend | Falls back to `console_log` + warning log |
| `AZURE_COMMUNICATION_SENDER_ADDRESS` | Key Vault | ACS backend | Falls back to `console_log` + warning log |
| `APPINSIGHTS_CONNECTION_STRING` | Container App env | OpenTelemetry | Telemetry disabled silently |
| `CORS_ALLOW_ORIGINS` | Container App env | CORS middleware | Empty list (no cross-origin allowed) |
| `APP_PUBLIC_URL` | Container App env | Email templates, share links | Defaults to `http://localhost:8000` |
| `TIER_STRIPE_PRICE_*` | Container App env | Billing | Tier checkout disabled (admin can still manually subscribe via Stripe dashboard) |
| `POINTS_*` | Container App env | Stage 8 ledger | Defaults from `core/config.py` |

---

## Swap-the-placeholder runbook (when you get real ACS creds)

1. Provision ACS + sender domain (one-time, §9 above).
2. Copy the real connection string from the Azure portal.
3. `az keyvault secret set --vault-name halalistic-kv --name AZURE-COMMUNICATION-CONNECTION-STRING --value "<real>"`
4. `az keyvault secret set --vault-name halalistic-kv --name AZURE-COMMUNICATION-SENDER-ADDRESS --value "Halalistic <noreply@halalistic.com>"`
5. `az containerapp revision restart -g $RG -n halalistic-api --revision <current>`
6. Curl `/api/v1/health` → `email_backend` will report `azure_acs`.
7. Trigger a password reset; the email should now reach the inbox.

---

## What is NOT in Stage 9 (deliberate)

- **SMS (Twilio).** `app/services/email/sms.py` exposes the
  `SmsBackend` Protocol so the extension point is explicit, but no
  provider is wired. Per BRD §3.8 + §9.2 (Backlog F-031) this is
  Phase 2.
- **Real VAPID subject CA registration.** The default subject is
  `mailto:ops@halalistic.com`. To upgrade to a vetted subject
  (mailto: with a custom domain), update `VAPID_CLAIMS_EMAIL` env.
- **Image rendering for non-deal entities.** Share card generator
  currently handles deals and restaurants. Phase 2 will add
  per-tag share cards and per-restaurant "halal cert" share cards.
- **Per-restaurant email opt-in.** New-deal marketing email
  currently goes to every diner. Phase 2 will add a per-restaurant
  opt-in flag on the diner side.
