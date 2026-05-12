import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_cartera_exec():
    db = get_db()
    
    doc = db["universo_cartera"].find_one({"ejecutivo": {"$exists": True, "$ne": ""}})
    if not doc:
        doc = db["universo_cartera"].find_one({"ejecutivo_asignado": {"$exists": True, "$ne": ""}})
        
    if doc:
        print("Found executive field in universo_cartera!")
        for k, v in doc.items():
            if "ejecutivo" in k.lower():
                print(f"Field: {k} = {v}")
    else:
        print("No executive field found in universo_cartera.")
        # Check all keys
        doc = db["universo_cartera"].find_one()
        if doc: print(f"Sample keys: {list(doc.keys())}")

if __name__ == "__main__":
    check_cartera_exec()
