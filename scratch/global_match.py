import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def find_global_matches():
    db = get_db()
    
    # Lead codes
    leads_codes = set(str(c) for c in db["leads"].distinct("prospecto.codigo") if c)
    print(f"Total unique Lead codes: {len(leads_codes)}")
    
    # Portfolio (yapo_propiedades)
    props = list(db["yapo_propiedades"].find({}, {"details.titulo": 1, "url": 1}))
    
    matches = 0
    matched_examples = []
    
    for p in props:
        t = p.get("details", {}).get("titulo", "")
        # Find all numbers of 4-7 digits
        nums = re.findall(r'\d{4,7}', t)
        
        # Also check URL
        url = p.get("url", "")
        nums += re.findall(r'\d{4,8}', url)
        
        for n in nums:
            if n in leads_codes:
                matches += 1
                matched_examples.append((n, t))
                break
                
    print(f"Total matches found: {matches}")
    if matched_examples:
        print("Sample matches:")
        for m in matched_examples[:10]:
            print(f"Code: {m[0]} -> Title: {m[1]}")

if __name__ == "__main__":
    find_global_matches()
