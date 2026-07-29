"""Email templates — small, dependency-free, shared across backends.

Each function returns `(subject, plain_text_body, html_body)`. The
plain text is the fallback for clients that don't render HTML; the
HTML is preferred when the backend supports it.
"""
from __future__ import annotations


def _wrap_html(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,system-ui,sans-serif;background:#fbf7ee;color:#1c1c1c;margin:0;padding:0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#1f5f3f;color:white;padding:24px 32px;">
  <tr><td><h1 style="margin:0;font-size:22px;letter-spacing:-0.01em;">Halalistic<span style="color:#d97a1a;">.</span></h1></td></tr>
</table>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:32px;">
  <tr><td style="max-width:560px;margin:0 auto;">
    {body_html}
  </td></tr>
</table>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:24px 32px;color:#888;font-size:12px;">
  <tr><td style="max-width:560px;margin:0 auto;">Halalistic — halal restaurant discovery + deals (Houston pilot).</td></tr>
</table>
</body></html>"""


def password_reset(*, raw_token: str, app_url: str) -> tuple[str, str, str]:
    reset_link = f"{app_url}/auth/reset-password?token={raw_token}"
    subject = "Reset your Halalistic password"
    body = (
        "Someone (hopefully you) requested a password reset.\n\n"
        f"Open this link to choose a new password (valid 1 hour, single use):\n{reset_link}\n\n"
        "If you didn't request this, ignore this email."
    )
    html_body = _wrap_html(subject, (
        f"<h2 style='margin:0 0 16px;'>Reset your password</h2>"
        f"<p>Click the button below to choose a new password. The link expires in 1 hour and can be used once.</p>"
        f"<p style='margin:24px 0;'>"
        f"<a href='{reset_link}' style='background:#1f5f3f;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;display:inline-block;'>Reset password</a>"
        f"</p>"
        f"<p style='color:#888;font-size:13px;'>If the button doesn't work, paste this link in your browser:<br><span style='word-break:break-all;'>{reset_link}</span></p>"
        f"<p style='color:#888;font-size:13px;'>If you didn't request this, ignore this email.</p>"
    ))
    return subject, body, html_body


def email_verification(*, raw_token: str, app_url: str) -> tuple[str, str, str]:
    verify_link = f"{app_url}/auth/verify-email?token={raw_token}"
    subject = "Verify your Halalistic email"
    body = (
        "Welcome to Halalistic!\n\n"
        f"Open this link to verify your email and unlock the full experience:\n{verify_link}\n\n"
        "The link is valid for 24 hours."
    )
    html_body = _wrap_html(subject, (
        f"<h2 style='margin:0 0 16px;'>Welcome to Halalistic</h2>"
        f"<p>Click the button below to verify your email. This unlocks the full Halalistic experience, including points, referrals, and saved restaurants.</p>"
        f"<p style='margin:24px 0;'>"
        f"<a href='{verify_link}' style='background:#d97a1a;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;display:inline-block;'>Verify email</a>"
        f"</p>"
        f"<p style='color:#888;font-size:13px;'>Link expires in 24 hours.</p>"
    ))
    return subject, body, html_body


def billing_receipt(*, amount_cents: int, description: str) -> tuple[str, str, str]:
    amount_str = f"${amount_cents/100:.2f}"
    subject = f"Receipt: {description} — {amount_str}"
    body = f"Thanks for being a Halalistic subscriber!\n\nCharge: {amount_str}\nFor: {description}\n"
    html_body = _wrap_html(subject, (
        f"<h2 style='margin:0 0 16px;'>Thanks for your payment</h2>"
        f"<p style='font-size:18px;'>Charge: <strong>{amount_str}</strong></p>"
        f"<p>For: {description}</p>"
        f"<p style='color:#888;font-size:13px;'>A copy of this receipt is available in your billing dashboard.</p>"
    ))
    return subject, body, html_body


def billing_payment_failed(*, amount_cents: int) -> tuple[str, str, str]:
    amount_str = f"${amount_cents/100:.2f}"
    subject = f"Payment failed: {amount_str}"
    body = (
        f"We couldn't process your most recent payment of {amount_str}.\n\n"
        "Stripe will retry over the next few days. Update your payment method to avoid service interruption:\n"
        f"{'https://app.halalistic.example/billing'}"
    )
    html_body = _wrap_html(subject, (
        f"<h2 style='margin:0 0 16px;'>Payment failed</h2>"
        f"<p>We couldn't process your most recent payment of <strong>{amount_str}</strong>. Stripe will retry automatically over the next few days.</p>"
        f"<p>To avoid service interruption, update your payment method in the Halalistic app.</p>"
    ))
    return subject, body, html_body


def new_deal_alert(*, deal_title: str, restaurant_name: str, share_url: str) -> tuple[str, str, str]:
    subject = f"New deal at {restaurant_name}: {deal_title}"
    body = (
        f"A new deal just dropped at {restaurant_name}:\n\n"
        f"  {deal_title}\n\n"
        f"View it here: {share_url}\n\n"
        f"You're getting this because you subscribed to deals at {restaurant_name}."
    )
    html_body = _wrap_html(subject, (
        f"<h2 style='margin:0 0 16px;'>New deal at {restaurant_name}</h2>"
        f"<p style='font-size:18px;font-weight:600;'>{deal_title}</p>"
        f"<p style='margin:24px 0;'>"
        f"<a href='{share_url}' style='background:#1f5f3f;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;display:inline-block;'>View deal</a>"
        f"</p>"
        f"<p style='color:#888;font-size:13px;'>You're getting this because you subscribed to deals at {restaurant_name}.</p>"
    ))
    return subject, body, html_body
