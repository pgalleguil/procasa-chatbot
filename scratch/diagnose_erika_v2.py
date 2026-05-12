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

def diagnose_erika_extended():
    db = get_db()
    regex_erika = re.compile("erika", re.IGNORECASE)
    
    # Query all leads related to Erika
    query = {
        "$or": [
            {"ejecutivo_asignado": regex_erika},
            {"prospecto.ejecutivo": regex_erika}
        ]
    }
    
    leads = list(db["leads"].find(query))
    print(f"Total leads found for 'Erika': {len(leads)}")
    
    print("\n--- Leads around May (April 25 to May 15) ---")
    count = 0
    for l in leads:
        created_at = _to_dt(l.get("created_at"))
        if not created_at:
            print(f"MISSING DATE: Phone: {l.get('phone')}, Assigned: {l.get('ejecutivo_asignado')}")
            continue
            
        if datetime(2026, 4, 25, tzinfo=CHILE_TZ) <= created_at <= datetime(2026, 5, 15, tzinfo=CHILE_TZ):
            count += 1
            phone = l.get("phone")
            ej_asig = l.get("ejecutivo_asignado")
            prosp_ej = l.get("prospecto", {}).get("ejecutivo")
            print(f"{count}. Phone: {phone}, Created: {created_at.isoformat()}, Assigned: {ej_asig}, Prospect Exec: {prosp_ej}")

    # Check for leads with Erika where phone might be missing
    print("\n--- Checking for leads with missing phone number ---")
    query_no_phone = {
        "$and": [
            {"$or": [{"ejecutivo_asignado": regex_erika}, {"prospecto.ejecutivo": regex_erika}]},
            {"$or": [{"phone": {"$exists": False}}, {"phone": ""}, {"phone": None}]}
        ]
    }
    no_phone_leads = list(db["leads"].find(query_no_phone))
    print(f"Leads for Erika with NO phone: {len(no_phone_leads)}")
    for l in no_phone_leads:
        print(f"ID: {l.get('_id')}, Created: {l.get('created_at')}, Assigned: {l.get('ejecutivo_asignado')}")

if __name__ == "__main__":
    diagnose_erika_extended()
