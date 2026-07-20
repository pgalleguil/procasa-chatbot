"""Non-delivering SLA evaluator for post-cutover canonical assignments."""
from __future__ import annotations

from collections import Counter

from .crm_metrics import INSTRUMENTATION_CUTOVER, calculate_sla, coerce_utc_datetime


def evaluate_sla_shadow(*, leads, cycles, users, deliveries, as_of) -> dict:
    now = coerce_utc_datetime(as_of)
    cutover = coerce_utc_datetime(INSTRUMENTATION_CUTOVER)
    if not now:
        raise ValueError("invalid SLA shadow as_of")
    lead_by_id = {str(lead.get("_id")): lead for lead in leads if lead.get("_id") is not None}
    active_users = {
        str(user.get("_id") or user.get("user_id") or user.get("nombre"))
        for user in users if user.get("active", user.get("activo", True))
    }
    delivered_cycles = {
        str((doc.get("metadata") or {}).get("assignment_cycle_id"))
        for doc in deliveries if doc.get("state") in {"sent", "delivered"}
    }
    rows, counters = [], Counter()
    seen_keys = set()
    cold_backlog = Counter()
    for cycle in cycles:
        lead_id = str(cycle.get("lead_id") or "")
        cycle_id = str(cycle.get("assignment_cycle_id") or "")
        recipient = str(cycle.get("assigned_to_user_id") or "")
        assigned = coerce_utc_datetime(cycle.get("assigned_at"))
        lead = lead_by_id.get(lead_id)
        if not lead_id or not cycle_id or not lead or not assigned:
            counters["quarantined"] += 1; continue
        if assigned < cutover:
            counters["pre_cutover_exclusions"] += 1; continue
        if cycle.get("unassigned_at") or recipient not in active_users:
            counters["inactive_executive_exclusions"] += 1; continue
        if cycle_id not in delivered_cycles:
            counters["delivery_failed_exclusions"] += 1; continue
        temperature = str(lead.get("lead_temperature_effective") or "").upper()
        sla = calculate_sla(
            assigned_at=assigned, first_valid_management_at=cycle.get("first_valid_management_at"), now=now,
        )
        if temperature != "HOT":
            if temperature == "COLD" and not sla["fulfilled"]:
                cold_backlog[recipient] += 1
            continue
        counters["eligible_sla_cycles"] += 1
        level = "red" if sla["status"] == "critical" else "yellow" if sla["status"] == "near_critical" else None
        if not level:
            continue
        key = f"{lead_id}|{cycle_id}|{level}|{recipient}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        counters[f"shadow_{level}"] += 1
        rows.append({
            "lead_id": lead_id, "assignment_cycle_id": cycle_id, "recipient_user_id": recipient,
            "level": level, "idempotency_key": key, "business_minutes": sla["minutes"],
            "shadow": True, "sent": False,
        })
    return {
        "as_of": now, "alerts": rows, "cold_backlog_by_executive": dict(cold_backlog),
        "eligible_sla_cycles": counters["eligible_sla_cycles"],
        "shadow_yellow": counters["shadow_yellow"], "shadow_red": counters["shadow_red"],
        "pre_cutover_exclusions": counters["pre_cutover_exclusions"],
        "inactive_executive_exclusions": counters["inactive_executive_exclusions"],
        "delivery_failed_exclusions": counters["delivery_failed_exclusions"],
        "quarantined": counters["quarantined"], "provider_calls": 0,
    }
