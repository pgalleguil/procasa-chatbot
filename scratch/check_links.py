import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import re

PROJECT_ROOT = Path(r"c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db

def check_link_events():
    db = get_db()
    # Look for events that might link to a property
    ev = db["crm_events"].find_one({"meta.property_id": {"$exists": True}})
    if ev:
        print(f"Found property_id in meta: {ev['meta']['property_id']}")
    else:
        # Check some recent events
        events = list(db["crm_events"].find({}).sort("timestamp", -1).limit(100))
        for e in events:
            meta = e.get("meta") or {}
            if "codigo" in str(meta) or "id" in str(meta):
                print(f"Event {e.get('type')} meta: {meta}")
                break

if __name__ == "__main__":
    check_link_events()
