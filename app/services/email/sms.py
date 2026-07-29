"""SMS extension point — STUB for Stage 9.

Per BRD §3.8 + §9.2 (Backlog item F-031), SMS is explicitly a Phase 2
task. We provide the Protocol + a clear NotImplementedError so the
extension point is documented and a future PR can drop in Twilio /
Vonage / Bandwidth / etc. without changing call sites.

DO NOT add an SMS provider SDK in Stage 9. The test
`test_sms_backend_raises_not_implemented` enforces this.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SmsMessage:
    to: str          # E.164 phone number
    body: str


class SmsBackend:
    """Protocol for an SMS provider. Stage 9 does NOT ship an implementation."""
    def send(self, msg: SmsMessage) -> None: ...


class SmsNotImplemented(SmsBackend):
    """Default stub. Calling .send() raises NotImplementedError so any
    accidental call fails loud in dev / staging, not silent.
    """
    def send(self, msg: SmsMessage) -> None:
        raise NotImplementedError(
            "SMS is a Phase 2 task per BRD §3.8 / §9.2 (Backlog item F-031). "
            "Wire Twilio / Vonage / Bandwidth here when ready."
        )
