import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_integer_code():
    db = get_db()
    code = 64053
    
    lead = db["leads"].find_one({"prospecto.codigo": code})
    print(f"Lead with code {code}: {lead.get('prospecto', {}).get('nombre') if lead else 'Not found'}")
    
    # Try string too just in case
    lead = db["leads"].find_one({"prospecto.codigo": str(code)})
    if lead: print(f"Lead with STRING code {code}: {lead.get('prospecto', {}).get('nombre')}")

    cartera = db["universo_cartera"].find_one({"codigo": code})
    print(f"Cartera with code {code}: {cartera.get('titulo') if cartera else 'Not found'}")
    
    yapo = db["yapo_propiedades"].find_one({"details.titulo": re.compile(str(code))})
    print(f"Yapo with code {code}: {yapo.get('details', {}).get('titulo') if yapo else 'Not found'}")

if __name__ == "__main__":
    check_integer_code()
