import os
import sys

# Add parent directory to path
sys.path.append(os.getcwd())

from pymongo import MongoClient
from config import Config

def check_phone_formats():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    print("--- Sample phones in 'leads' ---")
    leads = list(db["leads"].find({}, {"phone": 1}).limit(5))
    for l in leads:
        print(f"Phone: '{l.get('phone')}'")

    print("\n--- Sample phones in 'crm_events' ---")
    events = list(db["crm_events"].find({}, {"phone": 1}).limit(5))
    for e in events:
        print(f"Phone: '{e.get('phone')}'")

if __name__ == "__main__":
    check_phone_formats()
