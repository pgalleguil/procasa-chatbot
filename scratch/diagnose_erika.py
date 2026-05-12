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

def diagnose_erika():
    db = get_db()
    # Broad search for Erika
    regex_erika = re.compile("erika", re.IGNORECASE)
    
    query = {
        "$or": [
            {"ejecutivo_asignado": regex_erika},
            {"prospecto.ejecutivo": regex_erika}
        ]
    }
    
    leads = list(db["leads"].find(query))
    print(f"Total leads found for 'Erika' (any date): {len(leads)}")
    
    may_leads = []
    for l in leads:
        created_at = _to_dt(l.get("created_at"))
        if created_at and created_at.year == 2026 and created_at.month == 5:
            may_leads.append(l)
    
    print(f"Total leads for Erika in May 2026: {len(may_leads)}")
    
    for i, l in enumerate(may_leads):
        phone = l.get("phone")
        ej_asig = l.get("ejecutivo_asignado")
        prosp_ej = l.get("prospecto", {}).get("ejecutivo")
        created = l.get("created_at")
        print(f"{i+1}. Phone: {phone}, Created: {created}, Assigned: {ej_asig}, Prospect Exec: {prosp_ej}")

if __name__ == "__main__":
    diagnose_erika()
