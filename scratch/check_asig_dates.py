import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_erika_asig_dates():
    db = get_db()
    
    query = {"details.es_propietario_directo": True, "gestion.ejecutivo_asignado": "Erika Garrido"}
    docs = list(db["yapo_propiedades"].find(query, {"gestion.fecha_asignacion": 1, "created_at": 1}))
    
    has_asig = 0
    has_created = 0
    both_none = 0
    
    for d in docs:
        asig = d.get("gestion", {}).get("fecha_asignacion")
        created = d.get("created_at")
        if asig: has_asig += 1
        if created: has_created += 1
        if not asig and not created: both_none += 1
            
    print(f"Total: {len(docs)}")
    print(f"Has fecha_asignacion: {has_asig}")
    print(f"Has created_at: {has_created}")
    print(f"Both None: {both_none}")

if __name__ == "__main__":
    check_erika_asig_dates()
