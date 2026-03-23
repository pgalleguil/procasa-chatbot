
from pymongo import MongoClient
from config import Config
from datetime import datetime, timedelta
import pytz

CHILE_TZ = pytz.timezone('Chile/Continental')

def check_pending():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    all_notifs = list(db["pending_notifications"].find().sort("created_at", -1).limit(20))
    
    print(f"Recent notifications (any status):")
    for p in all_notifs:
        lead_data = p.get("lead_data", {})
        created_at = p.get("created_at")
        status = p.get("status")
        target_name = p.get("target_name") or lead_data.get("target_name")
        lead_phone = lead_data.get("phone")
        print(f"Time: {created_at} | Status: {status} | Target: {target_name} | Lead: {lead_phone}")

    # Check recent leads
    print("\n--- Recent Leads (Last 48 hours) ---")
    recent_leads = list(db["leads"].find().sort("created_at", -1).limit(10))
    
    for l in recent_leads:
        phone = l.get("phone")
        created = l.get("created_at")
        stage = l.get("stage")
        ejecutivo = l.get("ejecutivo_asignado") or l.get("prospecto", {}).get("ejecutivo")
        p_code = l.get("prospecto", {}).get("codigo") or l.get("meta", {}).get("property_code")
        print(f"Phone: {phone} | Created: {created} | Stage: {stage} | Exec: {ejecutivo} | Prop: {p_code}")

    # Also check biz hours logic
    now = datetime.now(CHILE_TZ)
    print(f"\nCurrent Chile Time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Weekday: {now.weekday()} (0=Mon, 6=Sun)")
    print(f"Hour: {now.hour}")
    
    from chatbot.constants import BUSINESS_START_HOUR, BUSINESS_END_HOUR, BUSINESS_DAYS
    is_business_day = now.weekday() in BUSINESS_DAYS
    is_in_hours = (now.hour >= BUSINESS_START_HOUR and now.hour < BUSINESS_END_HOUR)
    print(f"Business Start: {BUSINESS_START_HOUR}, End: {BUSINESS_END_HOUR}")
    print(f"Is Business Day: {is_business_day}")
    print(f"Is In Hours: {is_in_hours}")
    print(f"Should send now: {is_business_day and is_in_hours}")

if __name__ == "__main__":
    check_pending()
