import os
import sys

# Add parent directory to path
sys.path.append(os.getcwd())

from pymongo import MongoClient
from config import Config

def check_erika_fields():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    old_name = "Erika Garrido Varela"
    
    print(f"--- Checking for '{old_name}' in leads ---")
    
    count_asignado = db["leads"].count_documents({"ejecutivo_asignado": old_name})
    count_prospecto = db["leads"].count_documents({"prospecto.ejecutivo": old_name})
    
    print(f"Leads with 'ejecutivo_asignado' == '{old_name}': {count_asignado}")
    print(f"Leads with 'prospecto.ejecutivo' == '{old_name}': {count_prospecto}")

    if count_prospecto > 0:
        sample = db["leads"].find_one({"prospecto.ejecutivo": old_name})
        print(f"Sample lead phone: {sample.get('phone')}")
        print(f"Sample lead ejecutivo_asignado: {sample.get('ejecutivo_asignado')}")

if __name__ == "__main__":
    check_erika_fields()
