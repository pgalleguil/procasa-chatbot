import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_specific_code():
    db = get_db()
    code = "64053"
    
    lead = db["leads"].find_one({"prospecto.codigo": code})
    print(f"Lead with code {code}: {lead.get('prospecto', {}).get('nombre') if lead else 'Not found'}")
    
    cartera = db["universo_cartera"].find_one({"codigo": code})
    print(f"Cartera with code {code}: {cartera.get('titulo') if cartera else 'Not found'}")
    
    # Search in yapo_propiedades for this code string
    regex = re.compile(code)
    yapo = db["yapo_propiedades"].find_one({"details.titulo": regex})
    if yapo:
        print(f"Yapo with code {code} in title: {yapo.get('details', {}).get('titulo')}")
    else:
        # Try URL
        yapo = db["yapo_propiedades"].find_one({"url": regex})
        if yapo:
            print(f"Yapo with code {code} in URL: {yapo.get('url')}")
        else:
            print(f"Not found in Yapo by code {code}")

if __name__ == "__main__":
    check_specific_code()
