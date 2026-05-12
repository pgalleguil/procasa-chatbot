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

def check_rocio_sla():
    db = get_db()
    now = datetime.now(CHILE_TZ)
    regex_rocio = re.compile("rocio", re.IGNORECASE)
    
    query = {
        "$or": [{"ejecutivo_asignado": regex_rocio}, {"prospecto.ejecutivo": regex_rocio}]
    }
    
    leads = list(db["leads"].find(query))
    
    may_leads = []
    for l in leads:
        created_at = _to_dt(l.get("created_at"))
        if created_at and created_at.year == 2026 and created_at.month == 5:
            may_leads.append(l)
            
    print(f"Total leads for Rocío in May: {len(may_leads)}")
    
    violations = 0
    for i, l in enumerate(may_leads):
        assigned_at = _to_dt((l.get("lifecycle") or {}).get("assigned_at")) or _to_dt(l.get("created_at"))
        first_resp = _to_dt((l.get("lifecycle") or {}).get("first_response_at"))
        
        has_resp = first_resp is not None
        diff = (first_resp - assigned_at).total_seconds() / 60 if has_resp else (now - assigned_at).total_seconds() / 60
        
        is_sla_crit = diff > 180
        if is_sla_crit: violations += 1
        
        print(f"{i+1}. Phone: {l.get('phone')}, Assigned: {assigned_at.isoformat()}, HasResp: {has_resp}, DiffMin: {round(diff, 1)}, SLA Crit: {is_sla_crit}")

    print(f"\nTotal SLA Violations (new logic): {violations}")

if __name__ == "__main__":
    check_rocio_sla()
