import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def inspect_full_lead():
    db = get_db()
    lead = db["leads"].find_one({"prospecto.codigo": "64053"})
    if not lead:
        lead = db["leads"].find_one({"prospecto.codigo": {"$exists": True}})
        
    if lead:
        print("Full Lead structure:")
        import pprint
        pprint.pprint(lead)

if __name__ == "__main__":
    inspect_full_lead()
