import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_erika_office():
    db = get_db()
    regex_erika = re.compile("erika", re.IGNORECASE)
    doc = db["universo_cartera"].find_one({"ejecutivo": regex_erika})
    if doc:
        print(f"Erika's Office: {doc.get('oficina')}")
    else:
        print("Erika not found in universo_cartera.")

if __name__ == "__main__":
    check_erika_office()
