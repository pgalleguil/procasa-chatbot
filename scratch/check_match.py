import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def extract_code(title):
    if not title: return None
    # Try parentheses
    match = re.search(r'\((\d+)\)', title)
    if match: return match.group(1)
    # Try brackets
    match = re.search(r'\[([\w-]+)\]', title)
    if match: return match.group(1)
    return None

def check_match_rate():
    db = get_db()
    
    # Get all lead property codes
    leads_codes = db["leads"].distinct("prospecto.codigo")
    leads_codes = set([str(c) for c in leads_codes if c])
    print(f"Total unique codes in Leads: {len(leads_codes)}")
    
    # Get all yapo_propiedades and extract codes
    props = list(db["yapo_propiedades"].find({"details.es_propietario_directo": True}, {"details.titulo": 1, "url": 1}))
    
    found = 0
    matches = []
    for p in props:
        title = p.get("details", {}).get("titulo")
        code = extract_code(title)
        if not code:
            # Try URL ID
            url = p.get("url", "")
            match = re.search(r'/(\d+)$', url.strip("/"))
            if match: code = match.group(1)
            
        if code and code in leads_codes:
            found += 1
            matches.append(code)
            
    print(f"Total properties in Portfolio: {len(props)}")
    print(f"Properties with a matching Lead: {found}")
    if matches:
        print(f"Sample matches: {matches[:5]}")

if __name__ == "__main__":
    check_match_rate()
