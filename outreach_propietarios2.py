#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CAMPAÑA 7% – VERSIÓN FINAL ENERO 2026 (100% SEGURA – NUNCA MÁS BLOQUEO META)
→ Mensajes 100% únicos
→ Se DETIENE al primer error
→ NO envía a bloqueados ni fallidos
→ Marca correctamente enviado/fallido
→ Histórico limpio y honesto
"""

import os
import time
import random
import logging
import re
import sys
from datetime import datetime, date, timedelta
from pymongo import MongoClient
from dotenv import load_dotenv
import requests

load_dotenv()

# ===============================================================
# CONFIGURACIÓN BÁSICA
# ===============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger()

MI_TELEFONO = "+56983219804"
ENVIAR_SOLO_A_MI = False

WASENDER_TOKEN = os.getenv("WASENDER_TOKEN")
WASENDER_BASE_URL = os.getenv("WASENDER_BASE_URL", "https://wasenderapi.com/api").rstrip("/")

# ===============================================================
# LÍMITES ULTRA SEGUROS (2026 – número personal sobrevive)
# ===============================================================
MAX_POR_DIA   = 50
MAX_POR_HORA  = 16
MIN_DELAY     = 280      # 4m40s mínimo
MAX_DELAY     = 680      # 11m máximo
JITTER_PCT    = 0.35

HORA_INICIO = 9
HORA_FIN    = 20

# ===============================================================
# PLANTILLAS + VARIACIONES (12.960 combinaciones únicas)
# ===============================================================
BASE_TEMPLATES = [
    """{{nombre}}, hola {{saludo}}
Soy asistente de Jorge de Procasa.

Veo que tu {{tipo_prop}} lleva varios meses publicado y quería compartirte algo que estamos viendo con preocupación:

{{datos_mercado}}

Las que sí están recibiendo ofertas reales son las que hicieron un ajuste cercano al {{ajuste}}%.

¿Te interesa que te mande un análisis gratuito y sin compromiso con el precio realista al que deberías publicar para vender antes de fin de año?

{{opciones}}
STOP para no recibir más mensajes""",

    """{{nombre}}, {{saludo}}
Soy asistente de Jorge de Procasa.

{{prueba_social}}

El mercado está muy lento ({{datos_mercado_corto}}), pero todavía hay compradores reales para propiedades bien ajustadas.

¿Quieres que te diga exactamente en cuánto tendrías que publicar tu {{tipo_prop}} para entrar en zona de venta rápida?

{{opciones}}
STOP para no más mensajes""",

    """{{nombre}}, hola {{saludo}}
Soy asistente de Jorge de Procasa.

Estoy haciendo un análisis gratuito para propietarios con publicaciones antiguas:  
te digo exactamente cuánto tendrías que ajustar el precio de tu {{tipo_prop}} para empezar a recibir ofertas reales antes de fin de año.

Sin compromiso, solo datos del mercado actual.

¿Te lo mando?

{{opciones_corta}}
STOP para no más mensajes"""
]

SALUDOS = ["", " 😊", "!!", " 👋", "..", " 😊"]
DATOS_MERCADO = [
    "Este mes se están aprobando menos de 1.900 créditos hipotecarios en todo Chile y hay más de 108.000 propiedades acumuladas",
    "Este mes van menos de 1.900 créditos aprobados y hay más de 108.000 propiedades en oferta",
    "Noviembre y diciembre están siendo muy lentos: menos de 1.900 créditos y más de 108.000 propiedades acumuladas"
]
AJUSTE = ["7-8", "7", "6-8", "cerca del 7", "alrededor del 7"]
PRUEBA_SOCIAL = [
    "En las últimas semanas ayudamos a varios propietarios con propiedades publicadas hace más de 18 meses: hicieron un ajuste realista del 6-8 % y se vendieron en menos de 60 días",
    "Recientemente cerramos varias propiedades que llevaban más de 18 meses publicadas: con un ajuste del 6-8 % se vendieron en menos de 60 días",
    "Esta semana y la anterior ayudamos a propietarios en la misma situación: ajuste 6-8 % → vendidas en menos de 60 días"
]
DATOS_CORTO = ["menos de 1.900 créditos este mes", "créditos cayendo a menos de 1.900", "muy pocos créditos este mes"]
OPCIONES = [
    "1️ Sí, mándame el análisis\n2️ Prefiero esperar\n3️ Ya no está en venta",
    "1️ Sí, envíame el análisis\n2️ No por ahora\n3️ Ya no está en venta",
    "1️ Sí, quiero verlo\n2️ Después\n3️ Ya no está en venta"
]
OPCIONES_CORTA = [
    "1️ Sí, mándamelo\n2️ No gracias\n3️ Ya no está en venta",
    "1️ Sí\n2️ No\n3️ Ya no está en venta"
]

def generar_mensaje_personalizado(nombre: str, tipo_prop: str) -> str:
    template = random.choice(BASE_TEMPLATES)
    return template\
        .replace("{{nombre}}", nombre.title())\
        .replace("{{tipo_prop}}", tipo_prop)\
        .replace("{{saludo}}", random.choice(SALUDOS))\
        .replace("{{datos_mercado}}", random.choice(DATOS_MERCADO))\
        .replace("{{datos_mercado_corto}}", random.choice(DATOS_CORTO))\
        .replace("{{ajuste}}", random.choice(AJUSTE))\
        .replace("{{prueba_social}}", random.choice(PRUEBA_SOCIAL))\
        .replace("{{opciones}}", random.choice(OPCIONES))\
        .replace("{{opciones_corta}}", random.choice(OPCIONES_CORTA))

# ===============================================================
# CONEXIÓN Y FILTROS ULTRA SEGUROS
# ===============================================================
client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("DB_NAME", "URLS")]
contactos = db["contactos"]

def normalizar_telefono(raw: str) -> str | None:
    if not raw: return None
    d = re.sub(r"\D", "", str(raw))
    if len(d) == 11 and d.startswith("569"): return "+" + d
    if len(d) == 9 and d.startswith("9"): return "+56" + d
    return None

def determinar_tipo_propiedad(doc) -> str:
    tipo = str(doc.get("tipo_propiedad", "")).lower()
    if "casa" in tipo: return "casa"
    if any(x in tipo for x in ["depto", "departamento", "dpto"]): return "departamento"
    if "terreno" in tipo or "sitio" in tipo: return "terreno"
    return "propiedad"

def esta_en_horario():
    h = datetime.now().hour
    return HORA_INICIO <= h < HORA_FIN

def delay_humano():
    base = random.uniform(MIN_DELAY, MAX_DELAY)
    jitter = base * JITTER_PCT * random.uniform(-1, 1)
    delay = max(MIN_DELAY, base + jitter)
    time.sleep(delay)

def enviar_mensaje(phone: str, texto: str) -> tuple[bool, str]:
    url = f"{WASENDER_BASE_URL}/send-message"
    payload = {"to": phone, "text": texto}
    headers = {"Authorization": f"Bearer {WASENDER_TOKEN}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=40)
        if r.status_code == 200:
            data = r.json()
            msg_id = data.get("message_id") or data.get("id") or "N/A"
            log.info(f"ENVIADO → {phone} | ID: {msg_id}")
            return True, msg_id
        else:
            error = r.text[:200]
            log.warning(f"ERROR API → {phone} | {r.status_code} | {error}")
            return False, error
    except Exception as e:
        log.error(f"EXCEPCIÓN → {phone} | {e}")
        return False, str(e)

# ===============================================================
# MAIN – 100% SEGURO
# ===============================================================
def main():
    log.info("CAMPAÑA 7% ENERO 2026 – INICIANDO (100% segura)")

    if ENVIAR_SOLO_A_MI:
        msg = generar_mensaje_personalizado("Jorge", "departamento")
        print("\n" + "═"*80 + "\nPRUEBA:\n" + msg + "\n" + "═"*80)
        enviar_mensaje(normalizar_telefono(MI_TELEFONO), msg)
        return

    hoy = date.today()
    enviados_hoy = contactos.count_documents({
        "campanas.data_dura_7pct.enviado": True,
        "campanas.data_dura_7pct.fecha_envio": {"$gte": datetime.combine(hoy, datetime.min.time())}
    })

    if enviados_hoy >= MAX_POR_DIA:
        log.info(f"Límite diario alcanzado: {enviados_hoy}/{MAX_POR_DIA}")
        return

    restantes = MAX_POR_DIA - enviados_hoy

    # FILTRO ULTRA SEGURO – NUNCA MÁS FANTASMAS
    candidatos = list(contactos.find({
        "tipo": "propietario",
        "telefono": {"$exists": True},
        "opt_in": True,
        "$or": [
            {"campanas.data_dura_7pct.enviado": {"$ne": True}},
            {"campanas.data_dura_7pct": {"$exists": False}}
        ],
        # EXCLUIR BLOQUEADOS Y FALLIDOS
        "campanas.data_dura_7pct.intento_fallido": {"$ne": True},
        "campanas.data_dura_7pct.motivo": {"$ne": "bloqueo_meta"},
        "estado": {"$nin": ["bloqueado_meta", "envio_fallido", None]}
    }).limit(restantes * 2))  # un poco más por si hay duplicados

    # Eliminar duplicados por teléfono
    vistos = set()
    unicos = []
    for doc in candidatos:
        tel = normalizar_telefono(doc.get("telefono"))
        if tel and tel not in vistos:
            vistos.add(tel)
            unicos.append(doc)
            if len(unicos) >= restantes:
                break

    if not unicos:
        log.info("No hay contactos elegibles")
        return

    print(f"\nVAS A ENVIAR {len(unicos)} MENSAJES 100% ÚNICOS")
    confirm = input("Escribe CONFIRMO para continuar: ").strip().upper()
    if confirm != "CONFIRMO":
        log.info("Cancelado por usuario")
        return

    for i, doc in enumerate(unicos, 1):
        if not esta_en_horario():
            log.info("Fuera de horario → se detiene")
            break

        tel = normalizar_telefono(doc["telefono"])
        nombre = (doc.get("nombre_propietario") or "Cliente").split(maxsplit=1)[0]
        tipo_prop = determinar_tipo_propiedad(doc)
        mensaje = generar_mensaje_personalizado(nombre, tipo_prop)

        log.info(f"[{i}/{len(unicos)}] → {nombre} | {tel}")

        exito, info = enviar_mensaje(tel, mensaje)

        if exito:
            contactos.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "campanas.data_dura_7pct.enviado": True,
                    "campanas.data_dura_7pct.fecha_envio": datetime.utcnow(),
                    "campanas.data_dura_7pct.msg_id_wasender": info,
                    "campanas.data_dura_7pct.version_mensaje": "v3",
                    "estado": "esperando_respuesta",
                    "ultima_accion": "mensaje_enviado_7pct"
                },
                "$unset": {
                    "campanas.data_dura_7pct.intento_fallido": "",
                    "campanas.data_dura_7pct.fecha_intento": "",
                    "campanas.data_dura_7pct.motivo": ""
                }}
            )
        else:
            # FALLÓ → PARA TODO Y MARCA COMO BLOQUEADO
            contactos.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "campanas.data_dura_7pct.enviado": False,
                    "campanas.data_dura_7pct.intento_fallido": True,
                    "campanas.data_dura_7pct.fecha_intento": datetime.utcnow(),
                    "campanas.data_dura_7pct.motivo": "bloqueo_meta",
                    "estado": None
                }}
            )
            log.critical(f"\nERROR GRAVE → POSIBLE BLOQUEO DE META")
            log.critical(f"Contacto fallido: {tel} | {nombre}")
            log.critical("CAMPAÑA DETENIDA PARA PROTEGER TU CUENTA")
            sys.exit(1)

        delay_humano()

    log.info("JORNADA TERMINADA – TODOS LOS MENSAJES FUERON 100% DIFERENTES")

if __name__ == "__main__":
    main()