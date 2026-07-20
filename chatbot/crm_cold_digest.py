"""Cold digest simulation built only from universal shadow results."""
from __future__ import annotations

from collections import defaultdict
import hashlib


def _reference(lead_id) -> str:
    return "CRM-" + hashlib.sha256(str(lead_id).encode("utf-8")).hexdigest()[:8].upper()


def simulate_cold_digests(shadow_run, *, business_period, max_references=3,
                          abnormal_volume=50, crm_url="https://procasa-chatbot-yr8d.onrender.com/crm?temperature=COLD") -> dict:
    grouped = defaultdict(list)
    for row in shadow_run.get("results") or []:
        if row.get("temperature") == "COLD" and row.get("status") == "missing_notification":
            grouped[str(row.get("recipient_user_id"))].append(row)

    digests, anomalies = [], []
    for recipient, rows in grouped.items():
        if len(rows) > abnormal_volume:
            anomalies.append({"recipient_user_id": recipient, "reason": "abnormal_cold_volume", "count": len(rows)})
            continue
        refs = [_reference(row["lead_id"]) for row in rows[:max_references]]
        extra = max(0, len(rows) - len(refs))
        lines = [
            "📥 *Nuevos leads Cold asignados*", "",
            f"Tienes *{len(rows)} nuevos leads Cold* pendientes de revisión en el CRM.", "",
            *[f"• Referencia {ref}" for ref in refs],
        ]
        if extra:
            lines.extend(["", f"_{extra} adicionales están disponibles en el CRM._"])
        lines.extend(["", f"🔗 Revisar leads Cold: {crm_url}"])
        content = "\n".join(lines)
        digests.append({
            "recipient_user_id": recipient, "digest_type": "cold_assignment_digest_shadow",
            "business_period": business_period, "content_version": "cold_digest_v1",
            "lead_count": len(rows), "reference_count": len(refs), "backlog": len(rows),
            "length": len(content), "content": content, "simulated": True,
            "deliverable_record_created": False,
        })
    return {
        "digests": digests, "anomalies": anomalies, "simulated_count": len(digests),
        "blocked_count": len(anomalies), "suggested_frequency": None,
        "requires_volume_observation": True,
    }
