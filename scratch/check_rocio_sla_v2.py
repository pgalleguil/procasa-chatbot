import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db
from chatbot.constants import CHILE_TZ

def _to_dt(value):
    if not value: return None
    if isinstance(value, datetime): dt = value
    elif isinstance(value, str):
        raw = value.strip().replace("Z", "+00:00")
        try: dt = datetime.fromisoformat(raw)
        except: return None
    else: return None
    if dt.tzinfo is None: return CHILE_TZ.localize(dt)
    return dt.astimezone(CHILE_TZ)

def check_rocio_sla_v2():
    db = get_db()
    now = datetime.now(CHILE_TZ)
    
    # Check all leads in May first
    query_may = {
        "$or": [
            {"created_at": {"$regex": "^2026-05"}},
            {"created_at": {"$gte": datetime(2026, 5, 1)}}
        ]
    }
    leads = list(db["leads"].find(query_may))
    print(f"Total leads in May: {len(leads)}")
    
    rocio_leads = []
    for l in leads:
        ej = str(l.get("ejecutivo_asignado") or l.get("prospecto", {}).get("ejecutivo") or "").lower()
        if "roc" in ej:
            rocio_leads.append(l)
            
    print(f"Total leads for Rocío in May: {len(rocio_leads)}")
    
    violations = 0
    for i, l in enumerate(rocio_leads):
        assigned_at = _to_dt((l.get("lifecycle") or {}).get("assigned_at")) or _to_dt(l.get("created_at"))
        first_resp = _to_dt((l.get("lifecycle") or {}).get("first_response_at"))
        
        # Check for first management event if first_response_at is missing
        if not first_resp:
            phone = l.get("phone")
            if phone:
                ev = db["crm_events"].find_one({"phone": phone, "type": {"$in": ["GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD"]}}, sort=[("timestamp", 1)])
                if ev:
                    first_resp = _to_dt(ev.get("timestamp"))

        has_resp = first_resp is not None
        diff = (first_resp - assigned_at).total_seconds() / 60 if has_resp else (now - assigned_at).total_seconds() / 60
        
        is_sla_crit = diff > 180
        if is_sla_crit: violations += 1
        
        ej_name = l.get("ejecutivo_asignado") or l.get("prospecto", {}).get("ejecutivo")
        print(f"{i+1}. Exec: {ej_name}, HasResp: {has_resp}, DiffMin: {round(diff, 1)}, SLA Crit: {is_sla_crit}")

    print(f"\nTotal SLA Violations (new logic): {violations}")

if __name__ == "__main__":
    check_rocio_sla_v2()
