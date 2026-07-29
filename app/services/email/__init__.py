"""Email service — public API.

Stage 9 wires up a real email backend (Azure Communication Services)
behind a provider-agnostic interface. The factory in this module
picks the backend based on `settings.email_backend`:

  - "console_log"  (default; writes to structured log; safe for MVP)
  - "azure_acs"    (real sending via Azure Communication Services; needs
                    the connection string + verified sender in env — see
                    AZURE_DEPLOY_CHECKLIST.md for the deploy runbook)

SMS is explicitly NOT shipped per BRD §3.8 / §9.2. The extension point
(`app.services.email.sms.SmsBackend`) exists with a clear NotImplemented
error so the Phase 2 task is obvious.
"""
from __future__ import annotations

import logging
from typing import Protocol

from app.core.config import settings
from app.services.email.console_log import ConsoleLogBackend
from app.services.email.azure_acs import AzureACSBackend
from app.services.email.message import EmailMessage

__all__ = ["EmailMessage", "EmailBackend", "get_email_backend", "send",
           "send_password_reset", "send_email_verification",
           "send_billing_receipt", "send_billing_payment_failed",
           "send_new_deal_alert"]

logger = logging.getLogger("halalistic.email")


class EmailBackend(Protocol):
    """Provider-agnostic email-sending interface. Every backend implements
    `send(msg) -> None`. Errors must raise so the caller can decide
    whether to retry / log / alert.
    """
    def send(self, msg: EmailMessage) -> None: ...


_backend: EmailBackend | None = None


def get_email_backend() -> EmailBackend:
    """Lazy singleton. Pick the backend once per process based on env.
    If the requested backend is missing config, falls back to
    ConsoleLog with a loud warning (so prod never silently loses email).
    """
    global _backend
    if _backend is not None:
        return _backend
    name = (settings.email_backend or "console_log").lower()
    if name == "azure_acs":
        try:
            _backend = AzureACSBackend(
                connection_string=settings.azure_communication_connection_string,
                sender_address=settings.azure_communication_sender_address,
            )
            logger.info("email backend: AzureACS")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "AzureACS backend init failed (%s); falling back to console_log. "
                "Set AZURE_COMMUNICATION_CONNECTION_STRING + AZURE_COMMUNICATION_SENDER_ADDRESS "
                "to enable real sending. See AZURE_DEPLOY_CHECKLIST.md.",
                exc,
            )
            _backend = ConsoleLogBackend()
    else:
        _backend = ConsoleLogBackend()
        logger.info("email backend: ConsoleLog (no provider configured)")
    return _backend


def send(msg: EmailMessage) -> None:
    """Single send entry-point. Used by all the typed helpers below."""
    try:
        get_email_backend().send(msg)
    except Exception as exc:  # noqa: BLE001
        # Email is best-effort: never let an email failure cascade into
        # the user-facing request. Log loudly so ops sees it.
        logger.exception("email send failed (backend=%s to=%s subject=%s): %s",
                         settings.email_backend, msg.to, msg.subject, exc)


# ---- Typed helpers used by auth / billing / deals ----------------------
def send_password_reset(email_addr: str, raw_token: str, app_url: str) -> None:
    """Stage 2 helper, now real. Includes a clickable reset link."""
    from app.services.email.templates import password_reset
    subject, body, html = password_reset(raw_token=raw_token, app_url=app_url)
    send(EmailMessage(to=email_addr, subject=subject, body=body, html=html))


def send_email_verification(email_addr: str, raw_token: str, app_url: str) -> None:
    """Stage 9 — A trigger entry-point. Flips `User.email_verified=True`
    on consume, which fires the Stage 8 referral credit.
    """
    from app.services.email.templates import email_verification
    subject, body, html = email_verification(raw_token=raw_token, app_url=app_url)
    send(EmailMessage(to=email_addr, subject=subject, body=body, html=html))


def send_billing_receipt(email_addr: str, amount_cents: int, description: str) -> None:
    from app.services.email.templates import billing_receipt
    subject, body, html = billing_receipt(amount_cents=amount_cents, description=description)
    send(EmailMessage(to=email_addr, subject=subject, body=body, html=html))


def send_billing_payment_failed(email_addr: str, amount_cents: int) -> None:
    from app.services.email.templates import billing_payment_failed
    subject, body, html = billing_payment_failed(amount_cents=amount_cents)
    send(EmailMessage(to=email_addr, subject=subject, body=body, html=html))


def send_new_deal_alert(email_addr: str, deal_title: str, restaurant_name: str, share_url: str) -> None:
    from app.services.email.templates import new_deal_alert
    subject, body, html = new_deal_alert(
        deal_title=deal_title, restaurant_name=restaurant_name, share_url=share_url,
    )
    send(EmailMessage(to=email_addr, subject=subject, body=body, html=html))
