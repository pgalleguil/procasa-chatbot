"""CRM SLA Alert Sender — injectable transport with typed contract.

No imports from NotificationService, crm_hot_delivery, webhook, whatsapp_client.

Sender result types:
- confirmed_success: provider accepted → sent
- rejected_before_acceptance: provider explicitly rejected → retryable/final
- delivery_unknown: timeout, disconnect, ambiguous → delivery_uncertain
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SenderResult:
    outcome: str  # "confirmed_success" | "rejected_before_acceptance" | "delivery_unknown"
    provider_message_id: str | None = None
    error: str | None = None
    http_status: int | None = None


class SlaAlertSender(Protocol):
    async def __call__(self, phone: str, message: str) -> SenderResult:
        ...


class FakeSender:
    def __init__(self, outcome: str = "confirmed_success", provider_message_id: str = "fake-msg-001"):
        self.outcome = outcome
        self.provider_message_id = provider_message_id
        self._calls: list[dict] = []

    async def __call__(self, phone: str, message: str) -> SenderResult:
        self._calls.append({"phone": phone, "message": message})
        return SenderResult(
            outcome=self.outcome,
            provider_message_id=self.provider_message_id if self.outcome == "confirmed_success" else None,
        )


class FailingSender:
    def __init__(self, exc: Exception | None = None):
        self._exc = exc or TimeoutError("provider timeout")
        self._calls: list[dict] = []

    async def __call__(self, phone: str, message: str) -> SenderResult:
        self._calls.append({"phone": phone, "message": message})
        raise self._exc


async def _null_sender(phone: str, message: str) -> SenderResult:
    return SenderResult(outcome="delivery_unknown", error="live_send_disabled")


def get_sender(sender: SlaAlertSender | None = None) -> SlaAlertSender:
    if sender is not None:
        return sender
    from .whatsapp_client import send_whatsapp_message_detailed

    async def _live_sender(phone: str, message: str) -> SenderResult:
        result = await send_whatsapp_message_detailed(phone, message)
        if result.get("success") and result.get("provider_message_id"):
            return SenderResult(
                outcome="confirmed_success",
                provider_message_id=str(result["provider_message_id"]),
                http_status=result.get("http_status"),
            )
        return SenderResult(
            outcome="rejected_before_acceptance",
            error=result.get("error") or result.get("delivery_status") or "provider_rejected",
            http_status=result.get("http_status"),
        )

    return _live_sender
