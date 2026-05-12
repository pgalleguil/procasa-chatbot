import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_erika_phones_distribution():
    db = get_db()
    
    query = {"details.es_propietario_directo": True, "gestion.ejecutivo_asignado": "Erika Garrido"}
    docs = list(db["yapo_propiedades"].find(query, {"details.telefono": 1}))
    
    counts = {}
    for d in docs:
        ph = str(d.get("details", {}).get("telefono") or "").strip()
        counts[ph] = counts.get(ph, 0) + 1
        
    print("Phone distribution:")
    for ph, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        print(f"'{ph}': {count}")

if __name__ == "__main__":
    check_erika_phones_distribution()
