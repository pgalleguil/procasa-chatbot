import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_erika_unique_phones():
    db = get_db()
    
    query = {"details.es_propietario_directo": True, "gestion.ejecutivo_asignado": "Erika Garrido"}
    docs = list(db["yapo_propiedades"].find(query, {"details.telefono": 1}))
    
    phones = [str(d.get("details", {}).get("telefono") or "").strip() for d in docs]
    unique_phones = set(phones)
    
    print(f"Total docs: {len(docs)}")
    print(f"Unique phones: {len(unique_phones)}")
    print(f"Difference: {len(docs) - len(unique_phones)}")

if __name__ == "__main__":
    check_erika_unique_phones()
