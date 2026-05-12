import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_erika_8_details():
    db = get_db()
    
    # Find the ones with dates
    query = {"details.es_propietario_directo": True, "gestion.ejecutivo_asignado": "Erika Garrido"}
    docs = list(db["yapo_propiedades"].find(query))
    
    with_date = []
    without_date = []
    
    for d in docs:
        if d.get("gestion", {}).get("fecha_asignacion") or d.get("created_at"):
            with_date.append(d)
        else:
            without_date.append(d)
            
    print(f"Total: {len(docs)}")
    print(f"With Date: {len(with_date)}")
    print(f"Without Date: {len(without_date)}")
    
    print("\nPhones of 'With Date' ones:")
    for d in with_date:
        print(f"ID: {d['_id']}, Phone: {d.get('details', {}).get('telefono')}, Date: {d.get('gestion', {}).get('fecha_asignacion')}")

if __name__ == "__main__":
    check_erika_8_details()
