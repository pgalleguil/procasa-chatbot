import os, sys
from pymongo import MongoClient
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
load_dotenv()
client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("DB_NAME", "whatsapp_bot_db")]

phone = "56937505211"
lead = db.leads.find_one({"phone": {"$regex": phone}})

if lead:
    print(f"Phone: {lead.get('phone')}")
    print(f"lead_temperature: {lead.get('lead_temperature')}")
    print(f"last_intent: {lead.get('last_intent')}")
    print(f"last_intent_at: {lead.get('last_intent_at')}")
    print(f"pipeline_stage: {lead.get('pipeline_stage')}")
    print(f"stage: {lead.get('stage')}")
    print(f"ejecutivo_asignado: {lead.get('ejecutivo_asignado')}")
    print()
    print("=== messages (ultimos 6) ===")
    msgs = lead.get("messages", [])[-6:]
    for m in msgs:
        content = str(m.get('content', '')).encode('ascii', errors='replace').decode('ascii')[:200]
        print(f"  [{m.get('role')}]: {content}")
    print()
    print("=== TODOS LOS CAMPOS (no messages) ===")
    for k, v in lead.items():
        if k not in ['messages', '_id']:
            sv = str(v).encode('ascii', errors='replace').decode('ascii')[:100]
            print(f"  {k}: {sv}")
else:
    print(f"Lead {phone} no encontrado")
