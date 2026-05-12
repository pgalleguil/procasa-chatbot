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

def diagnose_captaciones_by_exec():
    db = get_db()
    
    docs = list(db["yapo_propiedades"].find(
        {"gestion.ejecutivo_asignado": {"$exists": True, "$ne": ""}},
        {"gestion.ejecutivo_asignado": 1, "details.es_propietario_directo": 1}
    ))
    
    stats = {}
    for d in docs:
        exec_name = _norm_exec_name(d.get("gestion", {}).get("ejecutivo_asignado"))
        is_direct = d.get("details", {}).get("es_propietario_directo") == True
        
        if exec_name not in stats:
            stats[exec_name] = {"total_assigned": 0, "direct": 0, "not_direct": 0}
        
        stats[exec_name]["total_assigned"] += 1
        if is_direct:
            stats[exec_name]["direct"] += 1
        else:
            stats[exec_name]["not_direct"] += 1

    df = pd.DataFrame.from_dict(stats, orient="index")
    print(df.sort_values("total_assigned", ascending=False))

if __name__ == "__main__":
    diagnose_captaciones_by_exec()
