import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_universo_cartera():
    db = get_db()
    doc = db["universo_cartera"].find_one()
    if doc:
        print("Structure of 'universo_cartera':")
        print(doc.keys())
        # Check for code/id
        for k, v in doc.items():
            print(f"Field: {k} = {v}")
            
    # Check for matches with leads
    leads_codes = set(str(c) for c in db["leads"].distinct("prospecto.codigo") if c)
    print(f"\nUnique codes in leads: {len(leads_codes)}")
    
    # Try common fields for codes in universo_cartera
    for code_field in ["codigo", "ID", "id_propiedad", "external_id", "id"]:
        cartera_codes = set(str(c) for c in db["universo_cartera"].distinct(code_field) if c)
        matches = leads_codes.intersection(cartera_codes)
        if matches:
            print(f"MATCHES FOUND in field '{code_field}': {len(matches)}")
            print(f"Sample matches: {list(matches)[:5]}")

if __name__ == "__main__":
    check_universo_cartera()
