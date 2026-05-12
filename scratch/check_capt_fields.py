import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_captacion_assignment_date():
    db = get_db()
    
    # Check one document to see the structure of 'gestion'
    doc = db["yapo_propiedades"].find_one({"gestion.ejecutivo_asignado": {"$exists": True, "$ne": ""}})
    if doc:
        print("Structure of 'gestion':")
        print(doc.get("gestion").keys())
        # Check for any field that looks like a date
        for k, v in doc.get("gestion").items():
            if "fecha" in k or "at" in k or "timestamp" in k:
                print(f"Found date field: {k} = {v}")
    else:
        print("No assigned captations found.")

if __name__ == "__main__":
    check_captacion_assignment_date()
