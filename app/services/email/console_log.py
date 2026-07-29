"""ConsoleLog backend — writes the email to the structured log + stdout.

Used by default (EMAIL_BACKEND=console_log) and as the automatic
fallback if a real backend (AzureACS) fails to initialize.

The raw token is NEVER logged in full (we only log a short prefix) so
the log doesn't become a token-leak vector during local dev.
"""
from __future__ import annotations

import logging
import re

from app.services.email.message import EmailMessage

logger = logging.getLogger("halalistic.email.console_log")

# Crude sanitizer: strip anything that looks like a 20+ char token
# string. Used for the body preview only.
_TOKEN_PAT = re.compile(r"\b[A-Za-z0-9_\-]{20,}\b")


class ConsoleLogBackend:
    def send(self, msg: EmailMessage) -> None:
        safe_body = _TOKEN_PAT.sub("<token-redacted>", msg.body)
        logger.info(
            "console_log_email to=%s subject=%s body=%s",
            msg.to, msg.subject, safe_body,
        )
        # Mirror to stdout for local dev (so devs see the email in their terminal).
        print(f"\n[email] to={msg.to}")
        print(f"[email] subject={msg.subject}")
        print(f"[email] body={safe_body}\n")
