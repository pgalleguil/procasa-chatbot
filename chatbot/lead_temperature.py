"""Canonical lead temperature writes.

Runtime readers must use ``lead_temperature_effective`` directly. Legacy signal
interpretation lives only here so migrations and write paths cannot disagree.
"""

from __future__ import annotations

import ast
import json
from typing import Any, Mapping


HOT = "HOT"
COLD = "COLD"
VALID_TEMPERATURES = {HOT, COLD}
HOT_INTENTS = {"ASK_VISIT", "ASK_CONTACT", "GIVE_OFFER"}
HOT_STAGES = {"VISIT_SCHEDULED", "VISIT_DONE", "OFFER", "NEGOTIATION"}
COMMERCIAL_ALERT_TYPES = {
    "InteresVisita",
    "SolicitudContacto",
    "EscaladoUrgente",
    "LeadHotWhatsapp",
}
CLOSED_STAGES = {"CLOSED_LOST", "CLOSED_WON"}


def normalize_temperature(value: Any) -> str:
    return HOT if str(value or "").strip().upper() == HOT else COLD


def normalize_alerts_sent(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(value)
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError, SyntaxError):
                continue
    return {}


def has_commercial_alert(value: Any) -> bool:
    alerts = normalize_alerts_sent(value)
    return any(alert_type in alerts for alert_type in COMMERCIAL_ALERT_TYPES)


def derive_effective_temperature(
    lead: Mapping[str, Any],
    *,
    overrides: Mapping[str, Any] | None = None,
) -> str:
    """Derive the canonical value for a write/backfill operation.

    This is intentionally not called while rendering or querying the CRM.
    """
    snapshot = dict(lead)
    if overrides:
        snapshot.update(overrides)

    prospecto = snapshot.get("prospecto") or {}
    intent = str(snapshot.get("last_intent") or "").upper()
    stage = str(snapshot.get("pipeline_stage") or snapshot.get("stage") or "").upper()
    alerts = normalize_alerts_sent(
        prospecto.get("alerts_sent") or snapshot.get("alerts_sent")
    )
    has_hot_signal = (
        intent in HOT_INTENTS
        or stage in HOT_STAGES
        or has_commercial_alert(alerts)
    )
    if has_hot_signal:
        return HOT

    previous = snapshot.get("lead_temperature_effective")
    if previous not in VALID_TEMPERATURES:
        previous = snapshot.get("lead_temperature")
    if normalize_temperature(previous) == HOT:
        if stage in CLOSED_STAGES or intent == "UNSUBSCRIBE":
            return COLD
        return HOT
    return COLD


def effective_temperature_set(value: Any) -> dict[str, str]:
    """Fields for write paths that already made an explicit classification."""
    normalized = normalize_temperature(value)
    return {
        "lead_temperature": normalized,
        "lead_temperature_effective": normalized,
    }
