import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_erika_dates_raw():
    db = get_db()
    
    query = {"details.es_propietario_directo": True, "gestion.ejecutivo_asignado": "Erika Garrido"}
    docs = list(db["yapo_propiedades"].find(query).limit(10))
    
    print("Raw dates for Erika's captations:")
    for d in docs:
        created = d.get("created_at")
        last_g = d.get("gestion", {}).get("fecha_ultima_gestion")
        print(f"ID: {d['_id']}, Created: {repr(created)}, LastG: {repr(last_g)}")

if __name__ == "__main__":
    check_erika_dates_raw()
