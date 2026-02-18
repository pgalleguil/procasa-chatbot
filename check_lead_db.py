from pymongo import MongoClient
import os
from bson.objectid import ObjectId

MONGO_URI = "mongodb+srv://pgalleguil:vLr5MTTZ7kcNzjSZ@cluster0.mzve39k.mongodb.net/?retryWrites=true&w=majority"
DB_NAME = "URLS"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]

print(f"Buscando en DB: {DB_NAME} (Cloud)")

# Caso reportado
phone = "56987937483"
# Limpiamos el teléfono por si acaso
phone_clean = "56987937483"

leads = list(db["leads"].find({"phone": {"$regex": phone_clean}}).sort("created_at", -1))

print(f"Leads encontrados para {phone}: {len(leads)}")
for l in leads:
    print("-" * 20)
    print(f"ID: {l.get('_id')}")
    print(f"Nombre: {l.get('nombre')} (Top level)")
    print(f"Email: {l.get('email')} (Top level)")
    print(f"Prospecto Nombre: {l.get('prospecto', {}).get('nombre')} (Prospecto)")
    print(f"Property Code: {l.get('prospecto', {}).get('codigo')}")
    print(f"Created At: {l.get('created_at')}")
    print(f"Source Type: {l.get('source_type')}")
    # Verificar si tiene datos_propiedad
    print(f"Tiene datos_propiedad: {'datos_propiedad' in l}")
    if 'datos_propiedad' in l:
        print(f"  - Codigo: {l['datos_propiedad'].get('codigo')}")
