"""Security primitives: password hashing (argon2id) and JWT signing (HS256).

Per BRD Section 7: never store or log plaintext passwords. Argon2id is the
modern default — bcrypt is also acceptable per the BRD; we pick argon2id
because it has stronger resistance to GPU/ASIC attacks with sensible defaults.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import settings

# ---- Password hashing ----
# argon2-cffi ships with OWASP-recommended defaults (time_cost=3, memory_cost=64MB,
# parallelism=4). Don't override unless you have a benchmark to justify it.
_ph = PasswordHasher()


def hash_password(plain: str) -> str:
    """Hash a plaintext password with argon2id. Never log the input."""
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against an argon2id hash. Returns False on mismatch."""
    try:
        return _ph.verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


# ---- JWT ----
JWT_ALGORITHM = "HS256"
TokenType = Literal["access", "refresh"]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def _decode(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])


def create_access_token(user_id: uuid.UUID, role: str) -> str:
    """Short-lived JWT for API requests (15 min default)."""
    now = _now_utc()
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    return _encode(payload)


def create_refresh_token(user_id: uuid.UUID, jti: uuid.UUID, family_id: uuid.UUID) -> str:
    """Long-lived JWT used solely to obtain new access tokens. Rotated on each use."""
    now = _now_utc()
    payload = {
        "sub": str(user_id),
        "jti": str(jti),
        "family_id": str(family_id),
        "type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=7)).timestamp()),
    }
    return _encode(payload)


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Decode a JWT, verifying signature + expiry + type. Raises on any failure."""
    payload = _decode(token)
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(
            f"expected token type {expected_type!r}, got {payload.get('type')!r}"
        )
    return payload


# ---- Password reset token (random opaque string, not a JWT) ----
def generate_password_reset_token() -> tuple[str, str]:
    """Return (raw_token, sha256_hex_hash) for storage.

    The raw token is emailed to the user (one-time, never stored).
    The hash is what we persist to verify the request.
    """
    raw = uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw, h


def hash_password_reset_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
