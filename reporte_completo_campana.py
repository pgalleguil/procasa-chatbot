#!/usr/bin/env python3
# reporte_completo_campana.py
# → REPORTE 100% COMPLETO DE LA CAMPAÑA EN UN SOLO COMANDO

from pymongo import MongoClient
from datetime import datetime, timezone
from config import Config

# ==============================================================================
# CONFIGURACIÓN (cambia solo esto si quieres ver otra campaña)
# ==============================================================================
NOMBRE_CAMPANA = "ajuste_precio_202512"

client = MongoClient(Config.MONGO_URI)
db = client[Config.DB_NAME]
collection = db[Config.COLLECTION_CONTACTOS]

def fmt(fecha):
    if not fecha: return "sin fecha"
    if isinstance(fecha, datetime):
        return fecha.astimezone().strftime("%d/%m %H:%M")
    return str(fecha)[:16]

print("\n" + "="*85)
print(f" REPORTE COMPLETO DE CAMPAÑA → {NOMBRE_CAMPANA} ".center(85, " "))
print("="*85 + "\n")

# 1. ENVÍOS TOTALES
enviados_query = {"update_price.campana_nombre": NOMBRE_CAMPANA}
total_enviados = collection.count_documents(enviados_query)

# 2. EXITOSOS vs FALLIDOS
exitosos = collection.count_documents({
    "update_price.campana_nombre": NOMBRE_CAMPANA,
    "update_price.canales.email.enviado": True
})

fallidos = collection.count_documents({
    "update_price.campana_nombre": NOMBRE_CAMPANA,
    "update_price.canales.email.enviado": False
})

# 3. RESPUESTAS
con_respuesta = collection.count_documents({
    "update_price.campana_nombre": NOMBRE_CAMPANA,
    "update_price.respuesta": {"$exists": True}
})

# 4. DETALLE DE RESPUESTAS
acciones = {}
cursor_acciones = collection.aggregate([
    {"$match": {"update_price.campana_nombre": NOMBRE_CAMPANA, "update_price.respuesta": {"$exists": True}}},
    {"$group": {"_id": "$update_price.respuesta.accion", "cantidad": {"$sum": 1}}}
])
for item in cursor_acciones:
    accion = item["_id"]
    texto = {
        "ajuste_7": "AJUSTE 7% (Recomendado)",
        "mantener": "Mantener precio actual",
        "no_disponible": "Ya vendida / No disponible",
        "llamada": "Quiere que lo llamen",
        "unsubscribe": "Darse de baja"
    }.get(accion, accion)
    acciones[accion] = {"cantidad": item["cantidad"], "texto": texto}

# ==============================================================================
# RESUMEN GENERAL
# ==============================================================================
print("RESUMEN GENERAL")
print("-" * 50)
print(f"Total enviados                   : {total_enviados}")
print(f"  → Enviados con éxito           : {exitosos}     (éxito)")
print(f"  → Rebotaron / Error            : {fallidos}     (fallo)")
print(f"Total con respuesta              : {con_respuesta}")
print(f"Sin respuesta aún                : {exitosos - con_respuesta}  (de los enviados con éxito)\n")

if con_respuesta > 0:
    print("RESPUESTAS DETALLADAS")
    print("-" * 50)
    for acc, data in acciones.items():
        print(f"  • {data['texto']:<30} → {data['cantidad']}")
    print()

# ==============================================================================
# LISTA DE REBOTADOS (con motivo del error)
# ==============================================================================
if fallidos > 0:
    print("CORREOS QUE REBOTARON O FALLARON")
    print("-" * 100)
    print(f"{'#' :<3} {'Nombre':<20} {'Email':<40} {'Código(s)':<20} {'Error'}")
    print("-" * 100)
    cursor_fallidos = collection.find({
        "update_price.campana_nombre": NOMBRE_CAMPANA,
        "update_price.canales.email.enviado": False
    }).sort("update_price.canales.email.fecha", -1)

    i = 1
    for doc in cursor_fallidos:
        email = doc.get("email_propietario", "sin email")
        nombre = (doc.get("nombre_propietario", "") or "Sin nombre").split()[0]
        codigos = ", ".join([p.get("codigo","?") for p in doc.get("propiedades",[])] ) or doc.get("codigo","?")
        error_msg = doc.get("update_price", {}).get("canales", {}).get("email", {}).get("error", "Error desconocido")
        # Limpiar mensaje largo
        if len(error_msg) > 80:
            error_msg = error_msg[:77] + "..."
        print(f"{i :<3} {nombre:<20} {email:<40} {codigos:<20} {error_msg}")
        i += 1
    print()

# ==============================================================================
# SIN RESPUESTA AÚN (los que abrieron pero no hicieron clic)
# ==============================================================================
sin_respuesta = exitosos - con_respuesta
if sin_respuesta > 0:
    print(f"AÚN NO RESPONDIERON ({sin_respuesta} propietarios)")
    print("-" * 80)
    print(f"{'Nombre':<20} {'Email':<40} {'Código(s)':<20} {'Enviado el'}")
    print("-" * 80)
    cursor_pendientes = collection.find({
        "update_price.campana_nombre": NOMBRE_CAMPANA,
        "update_price.canales.email.enviado": True,
        "update_price.respuesta": {"$exists": False}
    }).sort("update_price.canales.email.fecha", -1).limit(30)  # solo primeros 30

    for doc in cursor_pendientes:
        nombre = (doc.get("nombre_propietario", "") or "Sin nombre").split()[0]
        email = doc.get("email_propietario", "")
        codigos = ", ".join([p.get("codigo","?") for p in doc.get("propiedades",[])] ) or doc.get("codigo","?")
        fecha_envio = doc.get("update_price", {}).get("canales", {}).get("email", {}).get("fecha")
        print(f"{nombre:<20} {email:<40} {codigos:<20} {fmt(fecha_envio)}")
    if collection.count_documents({
        "update_price.campana_nombre": NOMBRE_CAMPANA,
        "update_price.canales.email.enviado": True,
        "update_price.respuesta": {"$exists": False}
    }) > 30:
        print("   ... y otros más ...")
    print()

print("REPORTE FINALIZADO")
print(f"Hora del reporte: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("\n¡Ejecuta este archivo cuando quieras para ver el estado actual en tiempo real!\n")