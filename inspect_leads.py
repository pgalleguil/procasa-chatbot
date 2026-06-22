from pymongo import MongoClient
from dotenv import load_dotenv
import os

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("DB_NAME", "whatsapp_bot_db")]

leads = db.leads.find({"bi_analytics_global": {"$exists": True}}).limit(10)
for l in leads:
    print("Phone:", l.get("phone"))
    print("RESULTADO_CHAT:", l.get("bi_analytics_global", {}).get("RESULTADO_CHAT"))
    print("lead_temperature:", l.get("lead_temperature"))
    print("-" * 30)
