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

def check_erika_monthly_capt():
    db = get_db()
    
    query = {"details.es_propietario_directo": True, "gestion.ejecutivo_asignado": "Erika Garrido"}
    docs = list(db["yapo_propiedades"].find(query))
    
    monthly_counts = {}
    for d in docs:
        last_gestion = _to_dt(d.get("gestion", {}).get("fecha_ultima_gestion"))
        created_at = _to_dt(d.get("created_at"))
        dt = last_gestion or created_at
        if dt:
            mk = dt.strftime("%Y-%m")
            monthly_counts[mk] = monthly_counts.get(mk, 0) + 1
        else:
            monthly_counts["SIN_FECHA"] = monthly_counts.get("SIN_FECHA", 0) + 1
            
    print("Monthly counts for Erika (Captaciones):")
    for mk in sorted(monthly_counts.keys()):
        print(f"{mk}: {monthly_counts[mk]}")
    print(f"Total: {len(docs)}")

if __name__ == "__main__":
    check_erika_monthly_capt()
