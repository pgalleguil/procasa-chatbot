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

def check_assigned_v_created():
    db = get_db()
    regex_erika = re.compile("erika", re.IGNORECASE)
    
    query = {
        "$or": [
            {"ejecutivo_asignado": regex_erika},
            {"prospecto.ejecutivo": regex_erika}
        ]
    }
    
    leads = list(db["leads"].find(query))
    
    print("Checking leads with assignment mismatch or near boundaries:")
    for l in leads:
        created_at = _to_dt(l.get("created_at"))
        assigned_at = _to_dt((l.get("lifecycle") or {}).get("assigned_at"))
        
        # If created in April but assigned in May
        if created_at and created_at.month == 4 and assigned_at and assigned_at.month == 5:
            print(f"CREATED APRIL, ASSIGNED MAY: Phone: {l.get('phone')}, Created: {created_at}, Assigned: {assigned_at}")
            
        # If created in May but assigned in June (unlikely since it's May 12)
        
        # Let's also check if there's any lead with 'erika' that has NO created_at but HAS assigned_at in May
        if not created_at and assigned_at and assigned_at.month == 5:
            print(f"NO CREATED DATE, ASSIGNED MAY: Phone: {l.get('phone')}, Assigned: {assigned_at}")

if __name__ == "__main__":
    check_assigned_v_created()
