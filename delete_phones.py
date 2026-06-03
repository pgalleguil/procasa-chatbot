import asyncio, sys
sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from config import Config
from motor.motor_asyncio import AsyncIOMotorClient

async def main():
    client = AsyncIOMotorClient(Config.MONGO_URI)
    coll = client['URLS']['yapo_propiedades']
    query = {'$or': [{'gestion.estado': 'NUEVO'}, {'gestion.estado': {'$exists': False}}]}
    update = {'$unset': {'whatsapp_phone': '', 'details.whatsapp_phone': '', 'details.vendedor_id': '', 'telefono': '', 'details.telefono': ''}}
    result = await coll.update_many(query, update)
    print(f'Propiedades actualizadas: {result.modified_count}')
    client.close()

asyncio.run(main())
