import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def extract_all_codes(text):
    if not text: return []
    # Find all 4-6 digit numbers in parentheses or brackets
    codes = re.findall(r'[\(\[](\d{4,6})[\)\]]', text)
    return codes

def find_matches_improved():
    db = get_db()
    
    # Get all unique codes from leads
    leads_docs = list(db["leads"].find({"prospecto.codigo": {"$exists": True, "$ne": ""}}, {"prospecto.codigo": 1}))
    leads_codes_list = [str(l.get("prospecto", {}).get("codigo")) for l in leads_docs]
    leads_codes_set = set(leads_codes_list)
    print(f"Total unique codes in Leads: {len(leads_codes_set)}")
    
    # Get portfolio properties
    props = list(db["yapo_propiedades"].find({"details.es_propietario_directo": True}, {"details.titulo": 1, "url": 1}))
    
    matches = 0
    prop_to_leads = {}
    
    for p in props:
        title = p.get("details", {}).get("titulo")
        codes = extract_all_codes(title)
        
        # Also try to find any 5 digit number in the title even without parentheses
        more_codes = re.findall(r'\b(\d{5})\b', title or "")
        all_possible = set(codes + more_codes)
        
        for c in all_possible:
            if c in leads_codes_set:
                matches += 1
                prop_to_leads.setdefault(c, []).append(p.get("_id"))
                break # count each prop once
                
    print(f"Total Portfolio properties: {len(props)}")
    print(f"Matches found: {matches}")
    if prop_to_leads:
        print(f"Example matched codes: {list(prop_to_leads.keys())[:10]}")

if __name__ == "__main__":
    find_matches_improved()
