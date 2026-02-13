import os
import sys

# Add parent directory to path
sys.path.append(os.getcwd())

from pymongo import MongoClient
from config import Config

def check_indexes():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    collections = ["leads", "crm_events", "universo_obelix", "crm_sla_warnings"]
    
    for coll_name in collections:
        print(f"\n--- Indexes for '{coll_name}' ---")
        try:
            indexes = list(db[coll_name].list_indexes())
            for idx in indexes:
                print(idx)
        except Exception as e:
            print(f"Error checking {coll_name}: {e}")

if __name__ == "__main__":
    check_indexes()
