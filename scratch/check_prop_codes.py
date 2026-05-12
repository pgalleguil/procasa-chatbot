import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_prop_codes():
    db = get_db()
    
    # Check leads to see codes
    lead = db["leads"].find_one({"prospecto.codigo": {"$exists": True, "$ne": ""}})
    if lead:
        print(f"Lead Property Code Example: {lead.get('prospecto', {}).get('codigo')}")
        
    # Check yapo_propiedades to see where the code is
    prop = db["yapo_propiedades"].find_one({"gestion.ejecutivo_asignado": {"$exists": True, "$ne": ""}})
    if prop:
        print("\nStructure of 'yapo_propiedades':")
        print(prop.keys())
        # Check for code-like fields
        for k in ["codigo", "id_propiedad", "external_id", "details"]:
            val = prop.get(k)
            if k == "details" and isinstance(val, dict):
                print(f"Details keys: {val.keys()}")
                if "codigo" in val: print(f"Found code in details: {val['codigo']}")
            elif val:
                print(f"Found field: {k} = {val}")

if __name__ == "__main__":
    check_prop_codes()
