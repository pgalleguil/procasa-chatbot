import argparse
from datetime import datetime, timedelta
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.append(ROOT)

from chatbot.storage import get_db, update_lead_state, save_pending_notification
from chatbot.lead_router import find_responsible_executive
from chatbot.constants import CHILE_TZ, UNASSIGNED_LABEL


ERROR_SNIPPET = "problema técnico momentáneo"
UNASSIGNED = {UNASSIGNED_LABEL, "No Asignado", "No asignado", "Sin Asignar", None, ""}


def _lead_has_grok_fallback(messages):
    for m in messages or []:
        if m.get("role") != "assistant":
            continue
        txt = str(m.get("content", "")).lower()
        if ERROR_SNIPPET in txt:
            return True
    return False


def recover(days=30, commit=False, notify=False, limit=500):
    db = get_db()
    since = datetime.now(CHILE_TZ) - timedelta(days=days)

    query = {
        "created_at": {"$gte": since.isoformat()},
        "stage": {"$nin": ["ARCHIVED", "REJECTED", "CLOSED_LOST", "CLOSED_WON"]},
        "$or": [
            {"ejecutivo_asignado": {"$in": list(UNASSIGNED)}},
            {"prospecto.ejecutivo": {"$in": list(UNASSIGNED)}},
        ],
    }

    projection = {
        "phone": 1,
        "created_at": 1,
        "messages": {"$slice": -20},
        "prospecto": 1,
        "codigo": 1,
        "comuna": 1,
        "zone": 1,
        "nombre": 1,
    }

    leads = list(db["leads"].find(query, projection).limit(limit))
    candidates = [l for l in leads if _lead_has_grok_fallback(l.get("messages", []))]

    processed = []
    for lead in candidates:
        phone = lead.get("phone")
        p = lead.get("prospecto", {}) or {}
        property_code = p.get("codigo") or lead.get("codigo")
        comuna = p.get("comuna") or lead.get("comuna")
        zone = lead.get("zone")
        nombre = p.get("nombre") or lead.get("nombre") or "Cliente"

        exec_name, exec_phone, assignment_type = find_responsible_executive(
            property_code=str(property_code) if property_code else None,
            comuna=comuna,
            zone=zone,
            lead_phone=phone,
            lead_name=nombre,
        )

        if not exec_name or exec_name == UNASSIGNED_LABEL:
            continue

        processed.append({
            "lead_id": str(lead.get("_id")),
            "phone": phone,
            "property_code": str(property_code) if property_code else "N/D",
            "executive": exec_name,
            "exec_phone": exec_phone,
            "assignment_type": assignment_type,
        })

        if not commit:
            continue

        now_cl = datetime.now(CHILE_TZ).isoformat()
        update_lead_state(phone, metadata={
            "ejecutivo_asignado": exec_name,
            "prospecto.ejecutivo": exec_name,
            "metodo_asignacion": "bulk_recover_grok_error",
            "assignment_type": assignment_type,
            "updated_at": now_cl,
            "recovered_from_grok_error": True,
        })

        if notify and exec_phone and exec_phone != "+56900000000":
            last_msg = ""
            msgs = lead.get("messages") or []
            if msgs:
                last_msg = msgs[-1].get("content", "")
            payload = {
                "phone": phone,
                "nombre": nombre,
                "property_code": str(property_code) if property_code else "N/D",
                "last_message": last_msg or "Lead recuperado tras incidente técnico.",
                "target_name": exec_name,
                "target_phone": exec_phone,
                "is_new_assignment": True,
            }
            save_pending_notification(payload)

    return leads, candidates, processed


def main():
    ap = argparse.ArgumentParser(description="Recupera leads afectados por fallback técnico de Grok.")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--notify", action="store_true")
    args = ap.parse_args()

    leads, candidates, processed = recover(
        days=args.days,
        commit=args.commit,
        notify=args.notify,
        limit=args.limit,
    )

    print(f"scan_total={len(leads)}")
    print(f"candidates_with_error={len(candidates)}")
    print(f"processable={len(processed)}")
    for r in processed[:30]:
        print(f"{r['phone']} | code={r['property_code']} | exec={r['executive']} | type={r['assignment_type']}")

    if not args.commit:
        print("dry_run_only=true")


if __name__ == "__main__":
    main()
