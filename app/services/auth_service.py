"""Auth business logic: registration, login, refresh-with-rotation, logout, password reset.

Refresh tokens use ROTATION with FAMILY-BASED REUSE DETECTION — this is the
common security gap in hand-rolled auth that the Stage 2 brief calls out.
Flow:
  1. /auth/login or /auth/register issues an access + refresh pair; a new
     'family_id' is created for that session.
  2. /auth/refresh validates the presented refresh token (not revoked, not
     expired, type=refresh), then marks it revoked and issues a new pair in
     the SAME family. The new refresh token's jti is recorded in
     `replaced_by_id` to form a chain.
  3. If a revoked refresh token is presented (theft / replay), we revoke
     the ENTIRE family. Every token issued in that chain becomes unusable,
     forcing the attacker (and the legitimate user) to re-authenticate.

See tests/test_auth_endpoints.py::test_refresh_reuse_revokes_family for the
end-to-end check.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_password_reset_token,
    hash_password,
    hash_password_reset_token,
    verify_password,
)
from app.models.enums import UserRole
from app.models.user import PasswordResetToken, RefreshToken, User
from app.services import email

REFRESH_TTL = timedelta(days=7)
RESET_TTL = timedelta(hours=1)


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 15 * 60  # access-token lifetime in seconds


@dataclass
class RegisterInput:
    email: str
    password: str
    display_name: str
    role: str  # "diner" or "restaurant_owner"


# ---------- Registration ----------
async def register_user(db: AsyncSession, inp: RegisterInput) -> User:
    if inp.role not in UserRole.signup_allowed():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"role {inp.role!r} cannot be self-assigned at signup; use admin endpoint",
        )

    existing = await db.scalar(select(User).where(User.email == inp.email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email already registered",
        )

    user = User(
        id=uuid.uuid4(),
        email=inp.email,
        password_hash=hash_password(inp.password),
        display_name=inp.display_name,
        role=UserRole(inp.role),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ---------- Login ----------
async def login(db: AsyncSession, email_addr: str, password: str) -> tuple[User, TokenPair]:
    user = await db.scalar(select(User).where(User.email == email_addr))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        # Single generic message: don't leak whether the email exists.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )
    pair = await _issue_token_pair(db, user)
    return user, pair


# ---------- Refresh with rotation + reuse detection ----------
async def refresh(db: AsyncSession, refresh_token: str) -> TokenPair:
    # Decode the JWT to learn the jti, family, user, exp.
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid refresh token",
        )

    jti = uuid.UUID(payload["jti"])
    user_id = uuid.UUID(payload["sub"])
    family_id = uuid.UUID(payload["family_id"])

    row = await db.get(RefreshToken, jti)
    if row is None or row.user_id != user_id or row.family_id != family_id:
        raise HTTPException(status_code=401, detail="refresh token not recognized")

    if row.expires_at <= _now_utc():
        raise HTTPException(status_code=401, detail="refresh token expired")

    if row.revoked_at is not None:
        # REUSE DETECTED: this token was already rotated. Treat as theft,
        # revoke the whole family and force re-login.
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_now_utc())
        )
        await db.commit()
        raise HTTPException(
            status_code=401,
            detail="refresh token reuse detected; family revoked",
        )

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user not available")

    # Rotate: mark this token revoked, then issue a new pair in same family.
    row.revoked_at = _now_utc()
    new_pair = await _issue_token_pair(db, user, family_id=family_id)
    new_jti = _extract_jti(new_pair.refresh_token)
    row.replaced_by_id = new_jti
    await db.commit()
    return new_pair


def _extract_jti(refresh_token: str) -> uuid.UUID:
    payload = decode_token(refresh_token, expected_type="refresh")
    return uuid.UUID(payload["jti"])


# ---------- Logout ----------
async def logout(db: AsyncSession, refresh_token: str) -> None:
    """Revoke a single refresh token (logout current device). Idempotent."""
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except Exception:
        return  # already invalid; logout is a no-op
    jti = uuid.UUID(payload["jti"])
    row = await db.get(RefreshToken, jti)
    if row is not None and row.revoked_at is None:
        row.revoked_at = _now_utc()
        await db.commit()


# ---------- Password reset ----------
async def request_password_reset(db: AsyncSession, email_addr: str) -> None:
    """Always respond OK (don't leak which emails exist). If the email is
    known, generate a one-time token and trigger the email stub.
    """
    user = await db.scalar(select(User).where(User.email == email_addr))
    if user is None or not user.is_active:
        return  # silent no-op

    raw, hashed = generate_password_reset_token()
    db.add(PasswordResetToken(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hashed,
        expires_at=_now_utc() + RESET_TTL,
    ))
    await db.commit()
    email.send_password_reset(email_addr, raw, app_url=settings.app_public_url)


async def confirm_password_reset(db: AsyncSession, raw_token: str, new_password: str) -> None:
    hashed = hash_password_reset_token(raw_token)
    row = await db.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hashed)
    )
    if row is None or row.used_at is not None or row.expires_at <= _now_utc():
        raise HTTPException(status_code=400, detail="invalid or expired reset token")
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="invalid or expired reset token")
    user.password_hash = hash_password(new_password)
    row.used_at = _now_utc()
    # Invalidate ALL refresh tokens for this user as a defensive measure.
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now_utc())
    )
    await db.commit()


# ---------- Stage 9: email verification (A trigger for Stage 8 referrals) ----------
EMAIL_VERIFY_TTL = timedelta(hours=24)


def _new_email_verify_token() -> tuple[str, str]:
    raw, hashed = generate_password_reset_token()  # reuse the secure token helper
    return raw, hashed


async def request_email_verification(db: AsyncSession, email_addr: str) -> None:
    """Generate + persist an email-verification token. The email is
    sent by the caller (auth endpoint). The token is the same shape as
    password-reset tokens (secure random + sha256 hash stored).
    """
    user = await db.scalar(select(User).where(User.email == email_addr))
    if user is None or not user.is_active:
        return  # silent no-op — don't leak which emails exist
    if user.email_verified:
        return  # already verified, nothing to do
    raw, hashed = _new_email_verify_token()
    db.add(PasswordResetToken(  # reuse the table — the column is generic "token"
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hashed,
        expires_at=_now_utc() + EMAIL_VERIFY_TTL,
    ))
    await db.commit()
    from app.services.email import send_email_verification
    send_email_verification(email_addr, raw, app_url=settings.app_public_url)


async def confirm_email_verification(db: AsyncSession, raw_token: str) -> Optional[User]:
    """Consume a verification token. Returns the verified User (for the
    endpoint to fire the Stage 8 A trigger) or None if the token is
    invalid / expired / already used.

    NB: We reuse `PasswordResetToken` as the table because the columns
    are identical (user_id, token_hash, expires_at, used_at, created_at)
    and the storage shape is the same. A future refactor can split
    these into a dedicated `email_verification_tokens` table without
    changing this API.
    """
    hashed = hash_password_reset_token(raw_token)
    row = await db.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hashed)
    )
    if row is None or row.used_at is not None or row.expires_at <= _now_utc():
        return None
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    user.email_verified = True
    row.used_at = _now_utc()
    await db.commit()
    return user


# ---------- Admin role assignment ----------
async def assign_role(db: AsyncSession, target_user_id: uuid.UUID, new_role: str) -> User:
    if new_role not in UserRole.admin_assignable():
        raise HTTPException(
            status_code=400,
            detail=f"role {new_role!r} cannot be assigned via this endpoint",
        )
    user = await db.get(User, target_user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    user.role = UserRole(new_role)
    await db.commit()
    await db.refresh(user)
    return user


# ---------- Helpers ----------
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _issue_token_pair(
    db: AsyncSession, user: User, family_id: uuid.UUID | None = None,
) -> TokenPair:
    """Issue an access + refresh pair, persisting the refresh row."""
    if family_id is None:
        family_id = uuid.uuid4()
    jti = uuid.uuid4()
    access = create_access_token(user.id, user.role.value)
    refresh = create_refresh_token(user.id, jti, family_id)
    db.add(RefreshToken(
        id=jti,
        user_id=user.id,
        family_id=family_id,
        expires_at=_now_utc() + REFRESH_TTL,
    ))
    await db.flush()  # ensure jti is registered before we link to it
    return TokenPair(access_token=access, refresh_token=refresh)
