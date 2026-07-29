"""Tests for Stage 4: halal certificates + certifying bodies + verification flow."""
import io
import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select

from app.core.security import hash_password
from app.models.certifying_body import CertifyingBody
from app.models.enums import (
    CertificateStatus,
    HalalStatus,
    HalalVerificationSource,
    UserRole,
)
from app.models.halal_certificate import HalalCertificate
from app.models.user import User
from app.services.certificates import CertificateService


# ---- Fake blob client (lets us test the round-trip without real Azure) ----
class FakeBlobClient:
    def __init__(self, store: "FakeBlobStore", container: str, name: str):
        self._store = store
        self._container = container
        self._name = name
        self.url = f"https://fake.blob.core.windows.net/{container}/{name}"

    async def upload_blob(self, data, overwrite=True, content_type=None):
        self._store.blobs[(self._container, self._name)] = data.read()

    async def download_blob(self):
        return _FakeStream(self._store.blobs[(self._container, self._name)])


class _FakeStream:
    def __init__(self, data: bytes):
        self._data = data
    async def readall(self):
        return self._data


class FakeBlobStore:
    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}

    def get_container_client(self, container: str):
        outer = self
        class _Container:
            def get_blob_client(self_inner, name: str):
                return FakeBlobClient(outer, container, name)
        return _Container()


def _make_pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%fake test pdf body for halalistic tests\n%%EOF\n"


@pytest_asyncio.fixture
def fake_blob_store(monkeypatch):
    """Inject a fake Azure blob store for tests that need upload/download.
    Auto-undoes after the test.
    """
    store = FakeBlobStore()

    def patched_init(self, connection_string=None, client=None):
        # Ignore the prod args; use the fake store.
        self._client = store
        self._container = "certificates"

    monkeypatch.setattr(CertificateService, "__init__", patched_init)
    return store


# ---- helpers ----
async def _seed_certifying_bodies_in(db) -> None:
    """Seed certifying bodies using the test's db session (so they're visible to client API calls)."""
    from scripts.seed import DEFAULT_CERTIFYING_BODIES
    for name, slug, country in DEFAULT_CERTIFYING_BODIES:
        existing = (await db.execute(
            select(CertifyingBody).where(CertifyingBody.slug == slug)
        )).scalar_one_or_none()
        if existing is not None:
            continue
        db.add(CertifyingBody(name=name, slug=slug, country=country, is_active=True))
    await db.commit()


async def _make_owner(db, email: str) -> User:
    u = User(
        id=uuid.uuid4(), email=email,
        password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=UserRole.RESTAURANT_OWNER,
    )
    db.add(u); await db.commit(); await db.refresh(u)
    return u


async def _make_admin(db, email: str) -> User:
    u = User(
        id=uuid.uuid4(), email=email,
        password_hash=hash_password("supersecret123"),
        display_name=email.split("@")[0], role=UserRole.PLATFORM_ADMIN,
    )
    db.add(u); await db.commit(); await db.refresh(u)
    return u


async def _login(client, email: str) -> str:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _make_restaurant(client, db, token: str, name: str = "Cert Test") -> str:
    r = await client.post(
        "/api/v1/restaurants",
        json={"name": name, "address_line": "1 St", "city": "Houston",
              "state": "TX", "postal_code": "77002", "country": "US",
              "price_range": "2", "cuisine_slugs": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---- Certifying body seed + listing ----
@pytest.mark.asyncio
async def test_seed_certifying_bodies_populates_defaults(db):
    """Idempotent seed: 8 default bodies on a fresh DB, 0 new on re-run."""
    await _seed_certifying_bodies_in(db)
    rows = (await db.execute(select(CertifyingBody))).scalars().all()
    assert len(rows) == 8
    slugs = {b.slug for b in rows}
    assert {"ifanca", "hms", "jakim", "mui", "muis", "iswa", "esma", "sfda"} == slugs
    # Idempotent: running again should add 0 new
    await _seed_certifying_bodies_in(db)
    rows2 = (await db.execute(select(CertifyingBody))).scalars().all()
    assert len(rows2) == 8


@pytest.mark.asyncio
async def test_admin_can_list_certifying_bodies(client: AsyncClient, db):
    await _seed_certifying_bodies_in(db)
    admin = await _make_admin(db, f"admin-cb-{uuid.uuid4().hex[:6]}@example.com")
    token = await _login(client, admin.email)
    r = await client.get(
        "/api/v1/admin/certifying-bodies",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    bodies = r.json()
    assert len(bodies) == 8
    assert any(b["slug"] == "ifanca" for b in bodies)


@pytest.mark.asyncio
async def test_admin_can_create_new_certifying_body(client: AsyncClient, db):
    admin = await _make_admin(db, f"admin-newcb-{uuid.uuid4().hex[:6]}@example.com")
    token = await _login(client, admin.email)
    r = await client.post(
        "/api/v1/admin/certifying-bodies",
        json={"name": "Test Cert Body", "slug": "test-cb", "country": "TT"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["slug"] == "test-cb"
    r2 = await client.post(
        "/api/v1/admin/certifying-bodies",
        json={"name": "Another", "slug": "test-cb", "country": None},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 409


# ---- Owner cert upload + blob round-trip (DoD-1a, 1d) ----
@pytest.mark.asyncio
async def test_owner_can_upload_cert_and_blob_roundtrips(client: AsyncClient, db, fake_blob_store):
    """DoD-1: document is confirmed encrypted at rest, not just stored.
    Azure SSE (AES-256, default-on) provides the encryption; we verify the
    DoD-1d contract — bytes are stored and recoverable intact.
    """
    await _seed_certifying_bodies_in(db)
    owner = await _make_owner(db, f"owner-cert-{uuid.uuid4().hex[:6]}@example.com")
    token = await _login(client, owner.email)
    rid = await _make_restaurant(client, db, token, "Upload Test")

    body = (await db.execute(
        select(CertifyingBody).where(CertifyingBody.slug == "ifanca")
    )).scalar_one()
    pdf = _make_pdf_bytes()

    r = await client.post(
        f"/api/v1/restaurants/{rid}/halal-certificate",
        data={
            "certifying_body_id": str(body.id),
            "issue_date": (date.today() - timedelta(days=30)).isoformat(),
            "expiry_date": (date.today() + timedelta(days=365)).isoformat(),
        },
        files={"file": ("ifanca_cert.pdf", io.BytesIO(pdf), "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    cert_out = r.json()
    assert cert_out["certifying_body_name"] == "Islamic Food and Nutrition Council of America"
    assert cert_out["status"] == "pending"
    # Blob URL points to Azure-style URL (proves it's stored, not local)
    assert cert_out["blob_url"].startswith("https://fake.blob.core.windows.net/")

    # DoD-1d: round-trip the bytes back — they must match exactly
    cert_row = (await db.execute(
        select(HalalCertificate).where(HalalCertificate.id == cert_out["id"])
    )).scalar_one()
    service = CertificateService()  # uses the fake_blob_store via monkeypatch
    downloaded = await service.download(cert_row.blob_name)
    assert downloaded == pdf, "downloaded bytes do not match uploaded bytes"


@pytest.mark.asyncio
async def test_owner_can_use_other_certifying_body(client: AsyncClient, db, fake_blob_store):
    """The "Other" option — owner types a free-text body name."""
    await _seed_certifying_bodies_in(db)
    owner = await _make_owner(db, f"owner-other-{uuid.uuid4().hex[:6]}@example.com")
    token = await _login(client, owner.email)
    rid = await _make_restaurant(client, db, token, "Other Test")
    pdf = _make_pdf_bytes()
    r = await client.post(
        f"/api/v1/restaurants/{rid}/halal-certificate",
        data={
            "custom_certifying_body": "My Local Mosque Halal Board",
            "issue_date": date.today().isoformat(),
        },
        files={"file": ("cert.pdf", io.BytesIO(pdf), "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["certifying_body_name"] == "My Local Mosque Halal Board"


@pytest.mark.asyncio
async def test_owner_cannot_upload_with_both_or_neither_body(client: AsyncClient, db, fake_blob_store):
    await _seed_certifying_bodies_in(db)
    owner = await _make_owner(db, f"owner-both-{uuid.uuid4().hex[:6]}@example.com")
    token = await _login(client, owner.email)
    rid = await _make_restaurant(client, db, token, "Both Test")
    pdf = _make_pdf_bytes()
    body = (await db.execute(select(CertifyingBody))).scalars().first()
    # Both → 400
    r = await client.post(
        f"/api/v1/restaurants/{rid}/halal-certificate",
        data={
            "certifying_body_id": str(body.id),
            "custom_certifying_body": "Also this",
            "issue_date": date.today().isoformat(),
        },
        files={"file": ("cert.pdf", io.BytesIO(pdf), "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    # Neither → 400
    r2 = await client.post(
        f"/api/v1/restaurants/{rid}/halal-certificate",
        data={"issue_date": date.today().isoformat()},
        files={"file": ("cert.pdf", io.BytesIO(pdf), "application/pdf")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_other_owner_cannot_upload_cert(client: AsyncClient, db, fake_blob_store):
    await _seed_certifying_bodies_in(db)
    a = await _make_owner(db, f"a-cert-{uuid.uuid4().hex[:6]}@example.com")
    b = await _make_owner(db, f"b-cert-{uuid.uuid4().hex[:6]}@example.com")
    a_token = await _login(client, a.email)
    b_token = await _login(client, b.email)
    rid = await _make_restaurant(client, db, a_token, "RBAC test")
    pdf = _make_pdf_bytes()
    r = await client.post(
        f"/api/v1/restaurants/{rid}/halal-certificate",
        data={"issue_date": date.today().isoformat()},
        files={"file": ("cert.pdf", io.BytesIO(pdf), "application/pdf")},
        headers={"Authorization": f"Bearer {b_token}"},
    )
    assert r.status_code == 403


# ---- Admin queue + review ----
@pytest.mark.asyncio
async def test_admin_sees_pending_queue(client: AsyncClient, db, fake_blob_store):
    await _seed_certifying_bodies_in(db)
    owner = await _make_owner(db, f"owner-q-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"admin-q-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    admin_token = await _login(client, admin.email)
    rid = await _make_restaurant(client, db, owner_token, "Queue Test")
    pdf = _make_pdf_bytes()
    body = (await db.execute(select(CertifyingBody))).scalars().first()
    r = await client.post(
        f"/api/v1/restaurants/{rid}/halal-certificate",
        data={"certifying_body_id": str(body.id), "issue_date": date.today().isoformat()},
        files={"file": ("cert.pdf", io.BytesIO(pdf), "application/pdf")},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    cert_id = r.json()["id"]
    r2 = await client.get(
        "/api/v1/admin/halal-certificates/pending",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 200, r2.text
    queue = r2.json()
    assert any(
        q["kind"] == "certificate" and q["certificate_id"] == cert_id
        for q in queue
    )


@pytest.mark.asyncio
async def test_admin_approves_cert_sets_restaurant_verified(client: AsyncClient, db, fake_blob_store):
    await _seed_certifying_bodies_in(db)
    owner = await _make_owner(db, f"owner-app-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"admin-app-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    admin_token = await _login(client, admin.email)
    rid = await _make_restaurant(client, db, owner_token, "Approve Test")
    pdf = _make_pdf_bytes()
    body = (await db.execute(select(CertifyingBody))).scalars().first()
    r = await client.post(
        f"/api/v1/restaurants/{rid}/halal-certificate",
        data={"certifying_body_id": str(body.id), "issue_date": date.today().isoformat()},
        files={"file": ("cert.pdf", io.BytesIO(pdf), "application/pdf")},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    cert_id = r.json()["id"]
    r2 = await client.post(
        f"/api/v1/admin/halal-certificates/{cert_id}/review",
        json={"approve": True, "review_notes": "Looks legit."},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "approved"
    r3 = await client.get(f"/api/v1/restaurants/{rid}")
    badge = r3.json()["halal_badge"]
    assert badge["status"] == "verified"
    assert badge["source"] == "certified"


@pytest.mark.asyncio
async def test_admin_rejects_cert_keeps_restaurant_pending(client: AsyncClient, db, fake_blob_store):
    await _seed_certifying_bodies_in(db)
    owner = await _make_owner(db, f"owner-rej-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"admin-rej-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    admin_token = await _login(client, admin.email)
    rid = await _make_restaurant(client, db, owner_token, "Reject Test")
    pdf = _make_pdf_bytes()
    body = (await db.execute(select(CertifyingBody))).scalars().first()
    r = await client.post(
        f"/api/v1/restaurants/{rid}/halal-certificate",
        data={"certifying_body_id": str(body.id), "issue_date": date.today().isoformat()},
        files={"file": ("cert.pdf", io.BytesIO(pdf), "application/pdf")},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    cert_id = r.json()["id"]
    r2 = await client.post(
        f"/api/v1/admin/halal-certificates/{cert_id}/review",
        json={"approve": False, "review_notes": "Document is illegible."},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "rejected"
    r3 = await client.get(f"/api/v1/restaurants/{rid}")
    assert r3.json()["halal_badge"]["status"] == "pending"


# ---- Self-reported path (DoD-4) ----
@pytest.mark.asyncio
async def test_owner_self_reported_path_no_cert_required(client: AsyncClient, db):
    """DoD-4: a restaurant with no formal certificate can still be marked
    self_reported and appear in search."""
    owner = await _make_owner(db, f"owner-sr-{uuid.uuid4().hex[:6]}@example.com")
    token = await _login(client, owner.email)
    rid = await _make_restaurant(client, db, token, "Self-Reported Test")
    r = await client.put(
        f"/api/v1/restaurants/{rid}/halal-source",
        json={"notes": "We are zabiha halal, no formal cert."},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    r2 = await client.get(f"/api/v1/restaurants/{rid}")
    badge = r2.json()["halal_badge"]
    assert badge["status"] == "pending"
    assert badge["source"] == "self_reported"
    r3 = await client.get("/api/v1/search/restaurants", params={"limit": 50})
    assert any(
        x["name"] == "Self-Reported Test" for x in r3.json()["results"]
    )


# ---- Expired certs query (DoD-3) ----
@pytest.mark.asyncio
async def test_expired_certs_query_returns_approved_past_expiry(client: AsyncClient, db, fake_blob_store):
    await _seed_certifying_bodies_in(db)
    owner = await _make_owner(db, f"owner-exp-{uuid.uuid4().hex[:6]}@example.com")
    admin = await _make_admin(db, f"admin-exp-{uuid.uuid4().hex[:6]}@example.com")
    owner_token = await _login(client, owner.email)
    admin_token = await _login(client, admin.email)
    rid = await _make_restaurant(client, db, owner_token, "Expired Test")
    pdf = _make_pdf_bytes()
    body = (await db.execute(select(CertifyingBody))).scalars().first()
    r = await client.post(
        f"/api/v1/restaurants/{rid}/halal-certificate",
        data={
            "certifying_body_id": str(body.id),
            "issue_date": (date.today() - timedelta(days=365)).isoformat(),
            "expiry_date": (date.today() - timedelta(days=1)).isoformat(),
        },
        files={"file": ("old.pdf", io.BytesIO(pdf), "application/pdf")},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    cert_id = r.json()["id"]
    r2 = await client.post(
        f"/api/v1/admin/halal-certificates/{cert_id}/review",
        json={"approve": True, "review_notes": "OK at the time."},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 200
    r3 = await client.get(
        "/api/v1/admin/halal-certificates/expired",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r3.status_code == 200, r3.text
    expired = r3.json()
    assert any(c["id"] == cert_id for c in expired)
