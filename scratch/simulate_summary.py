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

def simulate_per_exec():
    db = get_db()
    per_exec = {}
    
    # 1. Leads
    leads_docs = list(db["leads"].find({
        "$or": [
            {"ejecutivo_asignado": {"$exists": True, "$nin": ["", None]}},
            {"prospecto.ejecutivo": {"$exists": True, "$nin": ["", None]}},
        ]
    }))
    
    for lead in leads_docs:
        ejecutivo = _norm_exec_name(lead.get("ejecutivo_asignado") or lead.get("prospecto", {}).get("ejecutivo") or "SIN_ASIGNAR")
        per_exec.setdefault(ejecutivo, {"leads_asignados": 0})
        per_exec[ejecutivo]["leads_asignados"] += 1
        
    # 2. Captaciones
    captacion_docs = list(db["yapo_propiedades"].find(
        {"details.es_propietario_directo": True, "gestion.ejecutivo_asignado": {"$exists": True, "$ne": ""}}
    ))
    
    for doc in captacion_docs:
        g = doc.get("gestion") or {}
        ejecutivo = _norm_exec_name(g.get("ejecutivo_asignado") or "")
        if not ejecutivo: continue
        
        b = per_exec.setdefault(ejecutivo, {"leads_asignados": 0})
        b["capt_asignadas"] = b.get("capt_asignadas", 0) + 1
        
    print("Final per_exec for 'Erika Garrido':")
    print(per_exec.get("Erika Garrido"))

if __name__ == "__main__":
    simulate_per_exec()
