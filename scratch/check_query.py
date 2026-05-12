import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_script_query():
    db = get_db()
    
    # Exact query from script
    query = {"details.es_propietario_directo": True, "gestion.ejecutivo_asignado": {"$exists": True, "$ne": ""}}
    docs = list(db["yapo_propiedades"].find(query))
    
    erika_docs = [d for d in docs if "erika" in str(d.get("gestion", {}).get("ejecutivo_asignado", "")).lower()]
    
    print(f"Total docs found by script query: {len(docs)}")
    print(f"Total Erika docs found by script query: {len(erika_docs)}")
    
    # Let's check for case sensitivity in the 'True' value or if it's a string "true"
    query_string_true = {"details.es_propietario_directo": "true", "gestion.ejecutivo_asignado": {"$exists": True, "$ne": ""}}
    docs_string_true = list(db["yapo_propiedades"].find(query_string_true))
    print(f"Docs with string 'true': {len(docs_string_true)}")

if __name__ == "__main__":
    check_script_query()
