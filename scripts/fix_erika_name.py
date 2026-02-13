import os
import sys

# Add parent directory to path
sys.path.append(os.getcwd())

from pymongo import MongoClient
from config import Config

def fix_erika_name():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    old_name = "Erika Garrido Varela"
    new_name = "Erika Garrido"

    print(f"--- Normalizing executive name: '{old_name}' -> '{new_name}' ---")

    # 1. Update universo_obelix
    res_prop = db["universo_obelix"].update_many(
        {"ejecutivo": old_name},
        {"$set": {"ejecutivo": new_name}}
    )
    print(f"Properties updated in 'universo_obelix': {res_prop.modified_count}")

    # 2. Update leads
    res_leads = db["leads"].update_many(
        {"ejecutivo_asignado": old_name},
        {"$set": {"ejecutivo_asignado": new_name}}
    )
    print(f"Leads updated in 'leads': {res_leads.modified_count}")

    print("\n--- Done! ---")

if __name__ == "__main__":
    fix_erika_name()
