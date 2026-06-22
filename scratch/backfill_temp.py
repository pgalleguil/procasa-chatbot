import os
from pymongo import MongoClient
from dotenv import load_dotenv

# Load env
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    print("Error: MONGO_URI not found.")
    exit(1)

client = MongoClient(MONGO_URI)
db_name = os.getenv("DB_NAME", "whatsapp_bot_db")
db = client[db_name]

leads = db.leads.find({"lead_temperature": {"$exists": False}})
count = 0
for lead in leads:
    bi_res = lead.get("bi_analytics_global", {}).get("RESULTADO_CHAT", "EN_PROCESO")
    temp = "HOT" if bi_res in ["VISITA_SOLICITADA", "VISITA_AGENDADA", "CONTACTO_HUMANO"] else "COLD"
    
    db.leads.update_one(
        {"_id": lead["_id"]},
        {"$set": {"lead_temperature": temp}}
    )
    count += 1

print(f"Successfully backfilled lead_temperature for {count} leads.")
