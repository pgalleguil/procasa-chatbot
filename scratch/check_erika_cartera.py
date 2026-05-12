import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_erika_cartera():
    db = get_db()
    regex_erika = re.compile("erika", re.IGNORECASE)
    
    docs = list(db["universo_cartera"].find({"ejecutivo": regex_erika}))
    print(f"Erika's properties in universo_cartera: {len(docs)}")
    if docs:
        print(f"Sample: {docs[0].get('titulo')} - Code: {docs[0].get('codigo')}")

if __name__ == "__main__":
    check_erika_cartera()
