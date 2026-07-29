"""/auth/* endpoints — registration, login, refresh, logout, password reset."""
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import limiter
from app.db.session import get_db
from app.services.auth_service import (
    RegisterInput,
    TokenPair,
    confirm_email_verification,
    confirm_password_reset,
    login,
    logout,
    refresh,
    register_user,
    request_email_verification,
    request_password_reset,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ---- Schemas ----
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)
    role: Literal["diner", "restaurant_owner"] = "diner"


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class LogoutIn(BaseModel):
    refresh_token: str


class PasswordResetRequestIn(BaseModel):
    email: EmailStr


class PasswordResetConfirmIn(BaseModel):
    token: str = Field(min_length=16, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class EmailVerifyIn(BaseModel):
    token: str = Field(min_length=16, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


# ---- Endpoints ----
@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def register(
    request: Request,
    body: RegisterIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    ref: Annotated[Optional[str], Query(max_length=20)] = None,
) -> TokenOut:
    new_user = await register_user(
        db,
        RegisterInput(
            email=body.email,
            password=body.password,
            display_name=body.display_name,
            role=body.role,
        ),
    )
    # Stage 8: attach the referrer (if a valid `ref` code was supplied).
    # The actual referral credit fires when one of the A/C triggers
    # succeeds — NOT at registration, per BRD §3.7 ("crediting on
    # successful signup" requires a "successful" event beyond form
    # submission).
    if ref:
        from app.services import referrals as referrals_service
        await referrals_service.attach_referrer_on_register(
            db, new_user=new_user, ref_code=ref,
        )
    # Auto-login on register so the user gets a token pair back.
    _, pair = await login(db, body.email, body.password)
    return _to_out(pair)


@router.post("/login", response_model=TokenOut)
@limiter.limit("5/minute")
async def login_endpoint(
    request: Request,
    body: LoginIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenOut:
    _, pair = await login(db, body.email, body.password)
    return _to_out(pair)


@router.post("/refresh", response_model=TokenOut)
@limiter.limit("30/minute")
async def refresh_endpoint(
    request: Request,
    body: RefreshIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenOut:
    pair = await refresh(db, body.refresh_token)
    return _to_out(pair)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def logout_endpoint(
    request: Request,
    body: LogoutIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await logout(db, body.refresh_token)


@router.post("/password-reset/request", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("3/hour")
async def password_reset_request(
    request: Request,
    body: PasswordResetRequestIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await request_password_reset(db, body.email)


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def password_reset_confirm(
    request: Request,
    body: PasswordResetConfirmIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await confirm_password_reset(db, body.token, body.new_password)


@router.post("/verify-email", status_code=status.HTTP_200_OK)
@limiter.limit("10/minute")
async def verify_email(
    request: Request,
    body: EmailVerifyIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Stage 9: consume an email-verification token. On success, flips
    `User.email_verified = True` and fires the Stage 8 referral A
    trigger (referrer credits when the new user is email-verified).
    """
    user = await confirm_email_verification(db, body.token)
    if user is None:
        raise HTTPException(status_code=400, detail="invalid or expired token")
    from app.services import referrals as referrals_service
    await referrals_service.credit_referral_if_eligible(
        db, referred_user_id=user.id,
    )
    return {"email_verified": True, "user_id": str(user.id)}


def _to_out(p: TokenPair) -> TokenOut:
    return TokenOut(
        access_token=p.access_token,
        refresh_token=p.refresh_token,
        token_type=p.token_type,
        expires_in=p.expires_in,
    )
