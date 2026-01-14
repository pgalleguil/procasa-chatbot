from pymongo import MongoClient
from config import Config
from datetime import datetime

client = MongoClient(Config.MONGO_URI)
db = client[Config.DB_NAME]
col = db[Config.COLLECTION_CONTACTOS]

campana = "actualizacion_mercado_2026"

pipeline = [
    {"$match": {"update_price.respuesta_campana": campana}},
    {"$group": {
        "_id": "$update_price.accion",
        "cantidad": {"$sum": 1},
        "propietarios": {"$addToSet": "$email_propietario"}
    }},
    {"$project": {
        "accion": "$_id",
        "cantidad": 1,
        "propietarios_unicos": {"$size": "$propietarios"}
    }}
]

resultados = list(col.aggregate(pipeline))

print(f"REPORTE CAMPAÑA WHATSAPP - {campana} ({datetime.now().strftime('%d/%m/%Y')})\n")
print(f"{'Acción':<30} {'Cantidad':<10} {'Propietarios únicos'}")
print("-" * 60)
total = 0
for r in resultados:
    accion_texto = {
        "ajuste_7": "Ajuste 7% aceptado",
        "llamada": "Solicitud de llamada",
        "mantener": "Mantener precio",
        "no_disponible": "Propiedad no disponible"
    }.get(r["accion"], r["accion"])
    print(f"{accion_texto:<30} {r['cantidad']:<10} {r['propietarios_unicos']}")
    total += r["cantidad"]
print("-" * 60)
print(f"TOTAL RESPUESTAS: {total}")