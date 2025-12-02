# reporte_completo_campana.py → EL ÚNICO QUE FUNCIONA DE VERDAD (FECHA REAL 100%)

from pymongo import MongoClient
from config import Config
import pandas as pd
from datetime import datetime

client = MongoClient(Config.MONGO_URI)
db = client[Config.DB_NAME]
collection = db[Config.COLLECTION_CONTACTOS]

NOMBRE_CAMPANA = "ajuste_precio_202512"

pipeline = [
    {"$match": {"update_price.campana_nombre": NOMBRE_CAMPANA}},

    # Convertimos todo el documento a array para poder acceder al campo dinámico
    {"$addFields": {
        "doc_array": {"$objectToArray": "$$ROOT"},
        "nombre_completo": {
            "$concat": ["$nombre_propietario", " ", "$apellido_paterno_propietario", " ",
                        {"$ifNull": ["$apellido_materno_propietario", ""]}]
        }
    }},

    # Extraemos la fecha del objeto anidado "ajuste_precio_202512"
    {"$addFields": {
        "fecha_campana": {
            "$let": {
                "vars": {
                    "campo": {
                        "$arrayElemAt": [
                            {"$filter": {
                                "input": "$doc_array",
                                "cond": {"$eq": ["$$this.k", NOMBRE_CAMPANA]}
                            }},
                            0
                        ]
                    }
                },
                "in": {
                    "$getField": {
                        "field": "fecha_respuesta",
                        "input": {"$ifNull": ["$$campo.v", {}]}
                    }
                }
            }
        }
    }},

    # Elegimos la fecha definitiva
    {"$addFields": {
        "fecha_final": {
            "$switch": {
                "branches": [
                    {"case": {"$ne": ["$fecha_campana", None]}, "then": "$fecha_campana"},
                    {"case": {"$ne": ["$fecha_respuesta", None]}, "then": "$fecha_respuesta"},
                    {"case": {"$ne": ["$update_price.canales.email.fecha", None]}, "then": "$update_price.canales.email.fecha"}
                ],
                "default": None
            }
        },
        "fecha_mostrar": {
            "$dateToString": {
                "format": "%d-%m-%Y %H:%M",
                "date": "$fecha_final",
                "onNull": "Sin fecha"
            }
        }
    }},

    # Filtramos solo los que respondieron
    {"$match": {
        "$or": [
            {"ultima_accion": {"$regex": "ajuste_precio_202512$"}},
            {"ultima_accion": "unsubscribe"},
            {"fecha_campana": {"$ne": None}}
        ]
    }},

    # Clasificación final
    {"$addFields": {
        "respuesta_texto": {
            "$switch": {
                "branches": [
                    {"case": {"$regexMatch": {"input": "$ultima_accion", "regex": "ajuste_7"}}, "then": "ACEPTÓ AJUSTE 7%"},
                    {"case": {"$in": ["$ultima_accion", ["mantener", "mantener_ajuste_precio_202512"]]}, "then": "MANTENER PRECIO"},
                    {"case": {"$in": ["$ultima_accion", ["llamada", "llamada_ajuste_precio_202512"]]}, "then": "QUIERE QUE LO LLAMEN"},
                    {"case": {"$in": ["$ultima_accion", ["no_disponible", "no_disponible_ajuste_precio_202512"]]}, "then": "PROPIEDAD VENDIDA/NO DISPONIBLE"},
                    {"case": {"$eq": ["$ultima_accion", "unsubscribe"]}, "then": "SE DIO DE BAJA"},
                    {"case": {"$eq": [f"${NOMBRE_CAMPANA}.accion_elegida", "unsubscribe"]}, "then": "SE DIO DE BAJA"}
                ],
                "default": "OTRO"
            }
        }
    }},

    {"$sort": {"fecha_final": -1}}
]

total_enviados = collection.count_documents({
    "update_price.campana_nombre": NOMBRE_CAMPANA,
    "update_price.canales.email.enviado": True
})

respuestas = list(collection.aggregate(pipeline))

print(f"\n{'='*90}")
print(f"CAMPAÑA AJUSTE DE PRECIO - DICIEMBRE 2025")
print(f"{'='*90}")
print(f"Correos enviados       : {total_enviados}")
print(f"Respuestas recibidas   : {len(respuestas)}")
print(f"Tasa de respuesta      : {len(respuestas)/total_enviados*100:.1f}%\n")

aceptaron = sum(1 for x in respuestas if x['respuesta_texto'] == "ACEPTÓ AJUSTE 7%")
mantener  = sum(1 for x in respuestas if x['respuesta_texto'] == "MANTENER PRECIO")
llamada   = sum(1 for x in respuestas if x['respuesta_texto'] == "QUIERE QUE LO LLAMEN")
vendida   = sum(1 for x in respuestas if x['respuesta_texto'] == "PROPIEDAD VENDIDA/NO DISPONIBLE")
baja      = sum(1 for x in respuestas if x['respuesta_texto'] == "SE DIO DE BAJA")

print(f"Aceptaron ajuste 7%     : {aceptaron}  {'★'*aceptaron}")
print(f"Mantener precio         : {mantener}")
print(f"Quieren que los llamen  : {llamada}")
print(f"Vendida/no disponible   : {vendida}")
print(f"Se dieron de baja       : {baja}")
print(f"{'-'*50}\n")

for c in respuestas:
    print(f"{c['codigo']:6} | {c['nombre_completo']:42} | {c.get('telefono',''):14} | {c['respuesta_texto']:32} | {c['fecha_mostrar']}")

if respuestas:
    df = pd.DataFrame([{
        "Código": c["codigo"],
        "Nombre": c["nombre_completo"],
        "Teléfono": c.get("telefono", ""),
        "Email": c.get("email_propietario", ""),
        "Respuesta": c["respuesta_texto"],
        "Fecha Respuesta": c["fecha_mostrar"]
    } for c in respuestas])
    archivo = f"REPORTE_FINAL_REAL_{NOMBRE_CAMPANA}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    df.to_excel(archivo, index=False)
    print(f"\nEXCEL GUARDADO → {archivo}")

print(f"{'='*90}")