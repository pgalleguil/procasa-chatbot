import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def diagnose_captaciones():
    db = get_db()
    
    # Total assigned in yapo_propiedades
    total_assigned = db["yapo_propiedades"].count_documents({"gestion.ejecutivo_asignado": {"$exists": True, "$ne": ""}})
    
    # Total assigned and es_propietario_directo: True
    total_filtered = db["yapo_propiedades"].count_documents({
        "details.es_propietario_directo": True, 
        "gestion.ejecutivo_asignado": {"$exists": True, "$ne": ""}
    })
    
    # Total assigned and es_propietario_directo: NOT True
    total_not_direct = db["yapo_propiedades"].count_documents({
        "details.es_propietario_directo": {"$ne": True}, 
        "gestion.ejecutivo_asignado": {"$exists": True, "$ne": ""}
    })

    print(f"Total with executive assigned: {total_assigned}")
    print(f"Total with executive assigned AND es_propietario_directo=True: {total_filtered}")
    print(f"Total with executive assigned AND es_propietario_directo!=True: {total_not_direct}")

    if total_not_direct > 0:
        print("\nExamples of assigned but not 'propietario directo':")
        examples = list(db["yapo_propiedades"].find({
            "details.es_propietario_directo": {"$ne": True}, 
            "gestion.ejecutivo_asignado": {"$exists": True, "$ne": ""}
        }, {"details.titulo": 1, "details.es_propietario_directo": 1, "gestion.ejecutivo_asignado": 1}).limit(5))
        for ex in examples:
            print(ex)

if __name__ == "__main__":
    diagnose_captaciones()
