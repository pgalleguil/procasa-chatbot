#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CAMPANA MASIVA PROPIETARIOS - PROCASA 2025
Ahora con modo PRUEBA UN SOLO TELÉFONO o ENVÍO MASIVO
"""

import os
import time
import random
import logging
from datetime import datetime, timezone
from pymongo import MongoClient
from dotenv import load_dotenv
import requests

# ===================== CARGAR .env =====================
load_dotenv()

# ===================== VARIABLES QUE TÚ DEFINES =====================
# ←←←←←←←←←←←←←←←← CAMBIA AQUÍ SEGÚN LO QUE QUIERAS HACER ←←←←←←←←←←←←←←←←

MI_TELEFONO = "+56983219804"          # ← Tu teléfono para pruebas o envío único
ENVIAR_SOLO_A_MI = True               # ← True = solo a tu teléfono | False = envío masivo real

# Si pones ENVIAR_SOLO_A_MI = False → enviará a todos los propietarios pendientes
# (máximo 30 por ejecución, puedes cambiar abajo en MAX_ENVIOS)

# ===================== CONFIGURACIÓN =====================
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "URLS")
APICHAT_TOKEN = os.getenv("APICHAT_TOKEN")
APICHAT_CLIENT_ID = os.getenv("APICHAT_CLIENT_ID")
APICHAT_BASE_URL = os.getenv("APICHAT_BASE_URL", "https://api.apichat.io/v1")

MAX_ENVIOS = 30          # Solo aplica si ENVIAR_SOLO_A_MI = False
MIN_DELAY = 35
MAX_DELAY = 70

# ===================== TEMPLATE CON STOP =====================
TEMPLATE = """
Hola {{nombre}} 👋, soy asistente inmobiliaria de PROCASA Jorge Pablo Caro Propiedades.

Breve actualización: El mercado está presionado por factores que debemos considerar para la venta de tu propiedad. Te resumo la foto actual:

Sobre-Stock: Hay 108.000 viviendas disponibles (nivel histórico) y la velocidad de venta supera los 30 meses (CChC).
Freno Bancario: Las tasas siguen en el rango 4,5%–4,8%. Los bancos están pidiendo más pie y aprobando menos créditos.
Dato Clave: Un posible cambio político/económico traerá inversionistas, pero también más competencia de vendedores.

Mi recomendación: Posicionar tu propiedad como "oportunidad" AHORA mediante un ajuste técnico.

¿Cómo prefieres avanzar? (Responde con el número):

1️⃣ Ajustar precio (7%)
2️⃣ Mantener precio (tiempo de venta más largo)
3️⃣ Propiedad no disponible

Quedo atento a tu respuesta para gestionar de inmediato.

Responde STOP si prefieres no más notificaciones.
""".strip()

# ===================== LOGGING =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
log = logging.getLogger("CAMPANA")

# ===================== CONEXIÓN MONGO =====================
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
contactos = db["contactos"]

# ===================== ENVÍO =====================
def enviar_mensaje(phone: str, texto: str) -> dict:
    url = f"{APICHAT_BASE_URL}/sendText"
    payload = {"number": phone.replace("+", ""), "text": texto}
    headers = {
        "client-id": APICHAT_CLIENT_ID,
        "token": APICHAT_TOKEN,
        "Content-Type": "application/json"
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            msg_id = resp.json().get("id", "N/A")
            log.info(f"ENVIADO → {phone} | ID: {msg_id}")
            return {"success": True, "id": msg_id}
        else:
            log.error(f"ERROR {phone}: {resp.status_code} → {resp.text}")
            return {"success": False, "error": resp.text}
    except Exception as e:
        log.error(f"EXCEPCIÓN {phone}: {e}")
        return {"success": False, "error": str(e)}

# ===================== NORMALIZAR TELÉFONO =====================
def normalizar_telefono(raw: str) -> str | None:
    if not raw: return None
    s = str(raw).replace(" ", "").replace("-", "").replace("+", "")
    if len(s) == 9 and s.startswith("9"):
        s = "56" + s
    if len(s) == 11 and s.startswith("569") and s[3:].isdigit():
        return "+" + s
    return None

# ===================== MAIN =====================
def main():
    log.info("=== INICIANDO CAMPAÑA PROPIETARIOS ===")

    if ENVIAR_SOLO_A_MI:
        # MODO PRUEBA / ENVÍO ÚNICO
        log.info(f"MODO PRUEBA ACTIVADO → Solo envío a {MI_TELEFONO}")
        nombre = "Pablo (PRUEBA)"
        mensaje = TEMPLATE.replace("{{nombre}}", nombre)
        resultado = enviar_mensaje(MI_TELEFONO, mensaje + "\n\n(Mensaje de prueba - ignorar si no es real)")

        if resultado["success"]:
            log.info("¡Mensaje de prueba enviado correctamente!")
        else:
            log.error("Falló el envío de prueba")
    else:
        # MODO ENVÍO MASIVO REAL
        log.info(f"MODO MASIVO → Enviando hasta {MAX_ENVIOS} propietarios reales")

        query = {
            "tipo": "propietario",
            "campana_mercado_2025": {"$exists": False},
            "telefono": {"$exists": True, "$ne": None}
        }
        candidatos = list(contactos.find(query).limit(MAX_ENVIOS + 20))
        log.info(f"Propietarios pendientes encontrados: {len(candidatos)}")

        if not candidatos:
            log.info("No hay más propietarios pendientes. ¡Todo enviado!")
            return

        confirm = input(f"\nSe enviarán {min(len(candidatos), MAX_ENVIOS)} mensajes reales. ¿Confirmas? (sí/no): ").strip().lower()
        if confirm not in ["sí", "si", "s", "y", "yes"]:
            log.info("Campaña cancelada.")
            return

        enviados = 0
        for doc in candidatos:
            if enviados >= MAX_ENVIOS:
                break

            raw_phone = doc.get("telefono")
            nombre = (doc.get("nombre") or doc.get("cliente") or "Propietario").split()[0]
            telefono = normalizar_telefono(raw_phone)
            if not telefono:
                continue

            mensaje = TEMPLATE.replace("{{nombre}}", nombre.title())
            delay = random.uniform(MIN_DELAY, MAX_DELAY)
            log.info(f"Enviando a {nombre} ({telefono}) → {delay:.1f}s")
            time.sleep(delay)

            res = enviar_mensaje(telefono, mensaje)
            if res["success"]:
                contactos.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"campana_mercado_2025": {
                        "enviado": True,
                        "fecha": datetime.now(timezone.utc),
                        "msg_id": res["id"],
                        "telefono_usado": telefono
                    }}}
                )
                enviados += 1

        log.info(f"CAMPAÑA FINALIZADA → {enviados} mensajes enviados")

if __name__ == "__main__":
    main()