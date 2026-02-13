import os
import sys

# Add parent directory to path
sys.path.append(os.getcwd())

from pymongo import MongoClient
from config import Config

def investigate_leads_for_kpi():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    erika_leads = list(db["leads"].find({"ejecutivo_asignado": "Erika Garrido"}))
    print(f"--- Erika Garrido leads ({len(erika_leads)}) ---")
    
    for l in erika_leads:
        phone = l.get("phone")
        pc = phone.replace("+", "").strip() if phone else None
        print(f"\nLead: {phone}")
        print(f"Status fields: pipeline_stage={l.get('pipeline_stage')}, stage={l.get('stage')}, crm_estado={l.get('crm_estado')}")
        
        # Check for events
        management_types = [
            "GESTION_LOG", "HUMAN_NOTE", "SEND_WA_LEAD", "SEND_EMAIL_LEAD", 
            "CLICK_PHONE_LEAD", "SEND_WA_OWNER", "SEND_EMAIL_OWNER", "CLICK_PHONE_OWNER"
        ]
        events = list(db["crm_events"].find({"phone": pc, "type": {"$in": management_types}}).sort("timestamp", -1))
        print(f"Management events for {pc}: {len(events)}")
        for e in events:
            # Avoid unicode errors with encode-ignore
            msg = f"- Type: {e.get('type')}, Meta: {e.get('meta')}"
            print(msg.encode('ascii', 'ignore').decode('ascii'))

if __name__ == "__main__":
    investigate_leads_for_kpi()
