# api_reporte_real.py → CON CHECKBOX QUE SE GUARDA EN MONGO

from pymongo import MongoClient
from config import Config
from datetime import datetime

def get_reporte_real():
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    col = db[Config.COLLECTION_CONTACTOS]

    enviados = col.count_documents({
        "update_price.campana_nombre": {"$in": ["ajuste_precio_202512", "ajuste_precio_regiones_202512"]},
        "update_price.canales.email.enviado": True
    })

    respuestas_raw = list(col.find({
        "update_price.campana_nombre": {"$in": ["ajuste_precio_202512", "ajuste_precio_regiones_202512"]},
        "update_price.canales.email.enviado": True,
        "estado": {"$exists": True, "$nin": [None, ""]}
    }).sort("update_price.fecha_respuesta", -1))

    total_respuestas = len(respuestas_raw)

    contador = {
        "ajuste_autorizado": 0, "pendiente_llamada": 0, "precio_mantenido": 0,
        "no_disponible": 0, "suscripcion_anulada": 0
    }

    respuestas_limpias = []

    for r in respuestas_raw:
        estado = r.get("estado", "")
        if estado in contador:
            contador[estado] += 1

        nombre = f"{r.get('nombre_propietario','')} {r.get('apellido_paterno_propietario','')} {r.get('apellido_materno_propietario','')}".strip() or "Sin nombre"
        email = r.get("email_propietario", "").lower()

        fecha_raw = r.get("update_price", {}).get("fecha_respuesta")
        fecha_str = fecha_raw.strftime("%d/%m %H:%M") if isinstance(fecha_raw, datetime) else "Sin fecha"

        # AQUÍ ESTÁ LA MAGIA: leemos si ya está gestionado
        gestionado = r.get("gestionado", False)  # ← nuevo campo

        respuestas_limpias.append({
            "codigo": r.get("codigo", "S/C"),
            "nombre": nombre,
            "email": email,
            "telefono": r.get("telefono", ""),
            "respuesta": {
                "ajuste_autorizado": "ACEPTÓ 7%",
                "pendiente_llamada": "QUIERE QUE LO LLAMEN",
                "precio_mantenido": "MANTENER PRECIO",
                "no_disponible": "PROPIEDAD VENDIDA",
                "suscripcion_anulada": "SE DIO DE BAJA"
            }.get(estado, estado),
            "fecha": fecha_str,
            "gestionado": gestionado,           # ← NUEVO
            "email_para_update": email          # ← para el backend
        })

    return {
        "total_enviados": enviados,
        "total_respuestas": total_respuestas,
        "tasa_respuesta": round(total_respuestas / enviados * 100, 1) if enviados > 0 else 0,
        "aceptaron": contador["ajuste_autorizado"],
        "mantener": contador["precio_mantenido"],
        "llamada": contador["pendiente_llamada"],
        "vendida": contador["no_disponible"],
        "baja": contador["suscripcion_anulada"],
        "respuestas": respuestas_limpias
    }