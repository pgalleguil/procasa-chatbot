import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import sys

# Import config
sys.path.append(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from config import Config
from scraping_yapo_proxys import is_likely_broker

async def fix_brokers():
    client = AsyncIOMotorClient(Config.MONGO_URI)
    db = client[Config.DB_NAME] # Or whatever UI pulls from
    
    # Check if we should use URLS db or Config.DB_NAME map
    # Looking at scraping_yapo_proxys.py, url queue is in URLS db, 
    # but the extracted data goes to 'yapo_propiedades'
    coll = client["URLS"]["yapo_propiedades"]
    
    fixed_count = 0
    total = await coll.count_documents({"details.es_propietario_directo": True})
    print(f"Total owners to audit: {total}")
    
    async for prop in coll.find({"details.es_propietario_directo": True}):
        # Grab details
        details = prop.get("details", {})
        
        # Pull raw fields
        publicador = details.get("nombre_ejecutivo", "N/A")
        raw_desc = details.get("descripcion", "")
        company_name = details.get("nombre_corredora", "N/A")
        seller_type = details.get("tipo_vendedor", "N/A")
        
        # Same check from our scraper
        is_broker = (
            str(seller_type).lower() == "agente" or
            is_likely_broker(publicador, raw_desc, company_name)
        )
        
        if is_broker:
            # Fix it
            await coll.update_one(
                {"_id": prop["_id"]},
                {"$set": {
                    "details.es_propietario_directo": False,
                    "details.confianza_propietario": 1.0 # Broker -> 1.0
                }}
            )
            fixed_count += 1
            print(f"Fixed: {prop['url']}")

    print(f"Total entries patched: {fixed_count}")
    client.close()

if __name__ == "__main__":
    asyncio.run(fix_brokers())
