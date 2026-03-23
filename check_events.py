
from pymongo import MongoClient
from config import Config
from datetime import datetime, timedelta
import pytz

CHILE_TZ = pytz.timezone('Chile/Continental')

def check_events():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    # Check for recent events
    print("\n--- Recent CRM Events (Top 50) ---")
    recent_events = list(db["crm_events"].find({
        # "type": {"$in": ["msg_in", "assignment", "assignment_fail", "alert_sent"]}
    }).sort("timestamp", -1).limit(50))
    
    for e in recent_events:
        phone = e.get("phone")
        ts = e.get("timestamp")
        type_ = e.get("type")
        actor = e.get("actor")
        meta = e.get("meta", {})
        print(f"Time: {ts} | Type: {type_} | Actor: {actor} | Phone: {phone} | Meta: {meta}")

    # Check for logs in case of errors
    print("\n--- Background Tasks Status ---")
    status = db["background_tasks_status"].find_one() # If exists
    if status:
        print(status)
    else:
        print("No background_tasks_status found in DB.")

if __name__ == "__main__":
    check_events()
