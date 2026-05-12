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

def check_per_exec_keys():
    db = get_db()
    
    # Simulate the logic of build_report for the keys
    per_exec = {}
    
    # 1. Leads
    leads_docs = list(db["leads"].find({
        "$or": [
            {"ejecutivo_asignado": {"$exists": True, "$nin": ["", None]}},
            {"prospecto.ejecutivo": {"$exists": True, "$nin": ["", None]}},
        ]
    }, {"ejecutivo_asignado": 1, "prospecto.ejecutivo": 1}))
    
    for l in leads_docs:
        ej = _norm_exec_name(l.get("ejecutivo_asignado") or l.get("prospecto", {}).get("ejecutivo") or "SIN_ASIGNAR")
        per_exec.setdefault(ej, {"leads": 0, "capt": 0})
        per_exec[ej]["leads"] += 1
        
    # 2. Captaciones
    capt_docs = list(db["yapo_propiedades"].find(
        {"details.es_propietario_directo": True, "gestion.ejecutivo_asignado": {"$exists": True, "$ne": ""}},
        {"gestion.ejecutivo_asignado": 1}
    ))
    
    for d in capt_docs:
        ej = _norm_exec_name(d.get("gestion", {}).get("ejecutivo_asignado") or "")
        if not ej: continue
        per_exec.setdefault(ej, {"leads": 0, "capt": 0})
        per_exec[ej]["capt"] += 1
        
    print("Keys containing 'Erika':")
    for k, v in per_exec.items():
        if "erika" in k.lower():
            print(f"'{k}': Leads={v['leads']}, Captaciones={v['capt']}")

if __name__ == "__main__":
    check_per_exec_keys()
