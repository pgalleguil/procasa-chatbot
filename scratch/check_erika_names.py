import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def _norm_exec_name(v):
    return " ".join(str(v or "").strip().split())

def check_erika_variants():
    db = get_db()
    
    # Query all with Erika in the name
    regex_erika = re.compile("erika", re.IGNORECASE)
    
    docs = list(db["yapo_propiedades"].find(
        {
            "gestion.ejecutivo_asignado": regex_erika,
            "details.es_propietario_directo": True
        },
        {"gestion.ejecutivo_asignado": 1}
    ))
    
    counts = {}
    for d in docs:
        raw = d.get("gestion", {}).get("ejecutivo_asignado")
        norm = _norm_exec_name(raw)
        counts[raw] = counts.get(raw, 0) + 1
        
    print("Raw names and counts for 'Erika' (Direct=True):")
    for name, count in counts.items():
        print(f"'{name}' -> {count} (Normalized: '{_norm_exec_name(name)}')")
        
    print(f"\nTotal: {sum(counts.values())}")

if __name__ == "__main__":
    check_erika_variants()
