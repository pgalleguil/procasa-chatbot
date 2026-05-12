import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def inspect_leads_codes():
    db = get_db()
    leads = list(db["leads"].find({"prospecto.codigo": {"$exists": True, "$ne": ""}}, {"prospecto.codigo": 1, "prospecto.nombre": 1}).limit(20))
    print("Sample Lead Codes:")
    for l in leads:
        print(f"Code: {l.get('prospecto', {}).get('codigo')}")

if __name__ == "__main__":
    inspect_leads_codes()
