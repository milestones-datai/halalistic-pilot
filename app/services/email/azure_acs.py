"""Azure Communication Services (ACS) email backend — Stage 9.

Real implementation. Requires:
  - `AZURE_COMMUNICATION_CONNECTION_STRING`  (endpoint=...;accesskey=...)
  - `AZURE_COMMUNICATION_SENDER_ADDRESS`     (e.g. DoNotReply@<your-domain>.azurecomm.net)

If either is missing or the connection string still contains the
`PLACEHOLDER` literal we shipped, the init raises so the factory in
`__init__.py` falls back to `ConsoleLogBackend` with a loud warning.

To enable in production: see AZURE_DEPLOY_CHECKLIST.md.
"""
from __future__ import annotations

import logging

from azure.communication.email import EmailClient
from azure.core.exceptions import AzureError as _AzureError

from app.services.email.message import EmailMessage

logger = logging.getLogger("halalistic.email.azure_acs")


_PLACEHOLDER_MARKER = "PLACEHOLDER"


class AzureACSBackend:
    def __init__(self, *, connection_string: str, sender_address: str):
        if not connection_string or _PLACEHOLDER_MARKER in connection_string:
            raise RuntimeError(
                "AZURE_COMMUNICATION_CONNECTION_STRING is not set (still a placeholder). "
                "See AZURE_DEPLOY_CHECKLIST.md."
            )
        if not sender_address or _PLACEHOLDER_MARKER in sender_address:
            raise RuntimeError(
                "AZURE_COMMUNICATION_SENDER_ADDRESS is not set (still a placeholder). "
                "See AZURE_DEPLOY_CHECKLIST.md."
            )
        self._sender = sender_address
        self._client = EmailClient.from_connection_string(connection_string)
        logger.info("AzureACSBackend initialized (sender=%s)", sender_address)

    def send(self, msg: EmailMessage) -> None:
        content = {
            "subject": msg.subject,
            "plainText": msg.body,
        }
        if msg.html:
            content["html"] = msg.html
        message = {
            "senderAddress": self._sender,
            "recipients": {"to": [{"address": msg.to}]},
            "content": content,
        }
        try:
            poller = self._client.begin_send(message)
            result = poller.result()  # blocks until sent (or fails)
            logger.info("ACS sent to=%s subject=%s messageId=%s status=%s",
                        msg.to, msg.subject, getattr(result, "id", "?"),
                        getattr(result, "status", "?"))
        except _AzureError as exc:
            # Bubble up so the public `send()` wrapper can log + swallow.
            logger.error("ACS send failed: %s", exc)
            raise
