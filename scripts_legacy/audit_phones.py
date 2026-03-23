import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import sys
import os

# Add parent dir to path for config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

async def audit_data():
    print("Starting Data Audit...")
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    coll = db['yapo_propiedades']
    
    total = await coll.count_documents({"details.whatsapp_phone": {"$exists": True, "$ne": ""}})
    
    # Buscar registros que NO tienen 'phone_extraction_source' (los procesados antes de mi fix)
    risky_query = {
        "details.whatsapp_phone": {"$exists": True, "$ne": ""},
        "metadata.phone_extraction_source": {"$exists": False}
    }
    risky_count = await coll.count_documents(risky_query)
    
    # Buscar registros con el fix aplicado (para comparar)
    safe_count = await coll.count_documents({
        "metadata.phone_extraction_source": {"$exists": True}
    })

    print(f"\n--- DATA STATUS REPORT ---")
    print(f"Total leads with phone: {total}")
    print(f"Risky leads (Pre-fix): {risky_count}")
    print(f"Safe leads (Post-fix): {safe_count}")
    
    if total > 0:
        contamination_ratio = (risky_count / total) * 100
        print(f"Estimated Risk Ratio: {contamination_ratio:.1f}%")

    # Obtener ejemplo de uno riesgoso para inspección manual si se desea
    if risky_count > 0:
        sample = await coll.find_one(risky_query)
        print(f"\nSample risky URL: {sample.get('url')}")

    client.close()

if __name__ == "__main__":
    asyncio.run(audit_data())
