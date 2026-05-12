import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_office_status_fields():
    db = get_db()
    doc = db["universo_cartera"].find_one()
    if doc:
        print("Sample keys in universo_cartera:")
        print(list(doc.keys()))
        # Check for office-like fields
        for k in ["oficina", "sucursal", "team", "office", "status", "estado"]:
            if k in doc:
                print(f"Found field: {k} = {doc[k]}")
                
    # Check for specific office name
    regex_sucre = re.compile("sucre", re.IGNORECASE)
    doc_sucre = db["universo_cartera"].find_one({"$or": [
        {"oficina": regex_sucre},
        {"sucursal": regex_sucre},
        {"team": regex_sucre}
    ]})
    if doc_sucre:
        print("\nFound a property from SUCRE!")
        for k in ["oficina", "sucursal", "team"]:
            if k in doc_sucre: print(f"{k}: {doc_sucre[k]}")
    else:
        print("\nNo 'sucre' office field found by name search.")

if __name__ == "__main__":
    check_office_status_fields()
