import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_prop_details():
    db = get_db()
    
    docs = list(db["yapo_propiedades"].find({"details.es_propietario_directo": True}).limit(5))
    for d in docs:
        print(f"ID: {d['_id']}")
        print(f"URL: {d.get('url')}")
        print(f"Title: {d.get('details', {}).get('titulo')}")
        # Print all keys that have 'id' or 'codigo'
        for k, v in d.items():
            if "id" in k.lower() or "cod" in k.lower():
                print(f"Field: {k} = {v}")
        for k, v in d.get("details", {}).items():
            if "id" in k.lower() or "cod" in k.lower():
                print(f"Detail Field: {k} = {v}")
        print("-" * 20)

if __name__ == "__main__":
    check_prop_details()
