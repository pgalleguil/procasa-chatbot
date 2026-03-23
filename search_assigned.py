
from pymongo import MongoClient
from config import Config
from datetime import datetime, timedelta
import pytz

CHILE_TZ = pytz.timezone('Chile/Continental')

def search_assigned():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    today_str = "2026-03-23"
    
    print(f"Searching for leads with assigned_at containing '{today_str}'...")
    
    query = {
        "$or": [
            {"lifecycle.assigned_at": {"$regex": f"^{today_str}"}},
            {"prospecto.ejecutivo": {"$exists": True}}
        ]
    }
    
    # Let's just find everything with assigned_at and print the last few
    all_assigned = list(db["leads"].find({"lifecycle.assigned_at": {"$exists": True}}).sort("lifecycle.assigned_at", -1).limit(10))
    
    for l in all_assigned:
        phone = l.get("phone")
        assigned_at = l.get("lifecycle", {}).get("assigned_at")
        exec = l.get("ejecutivo_asignado")
        print(f"Phone: {phone} | Assigned At: {assigned_at} | Exec: {exec}")

if __name__ == "__main__":
    search_assigned()
