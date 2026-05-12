import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_cartera_backup_fields():
    db = get_db()
    doc = db["universo_cartera"].find_one({"codigo_procasa": {"$exists": True}})
    if doc:
        print("Found 'codigo_procasa' field in universo_cartera!")
    else:
        # Check for other similar fields
        doc = db["universo_cartera"].find_one()
        if doc:
            print(f"Sample keys: {list(doc.keys())}")

if __name__ == "__main__":
    check_cartera_backup_fields()
