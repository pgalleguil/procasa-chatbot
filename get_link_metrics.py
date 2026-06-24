import asyncio
from datetime import datetime, timedelta
import pytz
from pymongo import MongoClient
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config import Config

client = MongoClient(Config.MONGO_URI)
db = client[Config.DB_NAME]
coleccion = db["leads"]

query = {
    "$or": [
        {"prospecto.link_pendiente": True},
        {"prospecto.link_pendiente": "True"},
        {"prospecto.link_pendiente": "true"}
    ]
}

pendientes = list(coleccion.find(query))
print(f"Total encontrados: {len(pendientes)}")
print("-" * 50)

for p in pendientes:
    phone = p.get("phone", "Sin fono")
    mensajes = p.get("messages", [])
    link = "No se encontro"
    
    # Buscar el ultimo mensaje del usuario que tenga 'http' o un codigo largo
    for m in reversed(mensajes):
        if m.get("role") == "user":
            content = m.get("content", "")
            if "http" in content or "www" in content or any(c.isdigit() for c in content):
                link = content.replace("\n", " ")
                break
                
    print(f"Tel: {phone} | Msg: {link[:100]}")
