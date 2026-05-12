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

def check_assignment_count():
    db = get_db()
    regex_erika = re.compile("erika", re.IGNORECASE)
    
    query = {
        "$or": [
            {"ejecutivo_asignado": regex_erika},
            {"prospecto.ejecutivo": regex_erika}
        ]
    }
    
    leads = list(db["leads"].find(query))
    
    may_by_creation = 0
    may_by_assignment = 0
    
    for l in leads:
        created_at = _to_dt(l.get("created_at"))
        assigned_at = _to_dt((l.get("lifecycle") or {}).get("assigned_at")) or created_at
        
        if created_at and created_at.year == 2026 and created_at.month == 5:
            may_by_creation += 1
            
        if assigned_at and assigned_at.year == 2026 and assigned_at.month == 5:
            may_by_assignment += 1
            if not (created_at and created_at.year == 2026 and created_at.month == 5):
                print(f"Assigned in May but created in {created_at.month if created_at else 'N/A'}: {l.get('phone')}")

    print(f"Total May by creation: {may_by_creation}")
    print(f"Total May by assignment: {may_by_assignment}")

if __name__ == "__main__":
    check_assignment_count()
