
from pymongo import MongoClient
from config import Config
from datetime import datetime, timedelta
import pytz

CHILE_TZ = pytz.timezone('Chile/Continental')

def deep_check():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    now = datetime.now(CHILE_TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    print(f"Checking for activity on {today_start.date()}...")
    
    # Check leads from today
    # Since created_at is likely a string (ISO format), we'll search by prefix or regex
    today_str = today_start.date().isoformat()
    leads_today = list(db["leads"].find({"created_at": {"$regex": f"^{today_str}"}}))
    print(f"Leads created today: {len(leads_today)}")
    for l in leads_today:
        print(f"  Phone: {l.get('phone')} | Created: {l.get('created_at')} | Exec: {l.get('ejecutivo_asignado')}")

    # Check events from today
    events_today = list(db["crm_events"].find({"timestamp": {"$regex": f"^{today_str}"}}))
    print(f"Events today: {len(events_today)}")
    for e in events_today[:10]: # Limit to 10
        print(f"  Type: {e.get('type')} | Phone: {e.get('phone')} | TS: {e.get('timestamp')}")

    # Check routing state
    state = db["lead_routing_state"].find_one({"id": "jpc_rm_round_robin"})
    print(f"\nRouting State (Round Robin): {state}")

if __name__ == "__main__":
    deep_check()
