#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# outreach.py: Script para enviar outreach inicial a leads de ordenes_visitas vía Apichat.io
# (v13 + ANTI-BLOQUEO: Rate limiting, templates variados, límite diario, footer opt-out | Sin simulación)

import requests
import json
import random
import time  # Para delays
from datetime import datetime, timezone
from config import Config
from db import client, db  # Reusa tu conexión Mongo

config = Config()

# ========================================
# CONFIGURACIÓN PRINCIPAL (Fácil de Editar)
# ========================================
MAX_MENSAJES_DIARIO = 20  # Límite GLOBAL de envíos por día (anti-spam WhatsApp)
MIN_DELAY_BETWEEN_MSGS = 30  # Segundos mínimo entre envíos (random 30-60s para humano)
MAX_DELAY_BETWEEN_MSGS = 60

# Templates variados (elige random para <50% similitud)
TEMPLATES = [
    """
Hola {cliente}, soy asistente inmobiliaria de PROCASA Jorge Pablo Caro Propiedades. 😊 

Recordamos que hace poco mostraste interés en {prop_desc} y contactaste a uno de nuestros ejecutivos. ¿Pudiste coordinar y visitar alguna opción que te gustara? ¿Qué te pareció la experiencia?

Si sigues en la búsqueda de tu hogar ideal, me encantaría saber qué estás priorizando ahora: ¿dormitorios, comuna, presupuesto? ¡Estoy aquí para mostrarte opciones que se ajusten perfecto a lo que buscas!

Cuéntame un poco más para reconectarte con lo mejor de nuestra cartera.

Responde STOP para no recibir más mensajes.
    """,
    """
¡Hola {cliente}! Soy el asistente de PROCASA Jorge Pablo Caro Propiedades. 😊 

Hace poco viste {prop_desc} y hablaste con uno de nuestros asesores. ¿Lograste agendar una visita? ¿Cómo fue?

Si aún buscas tu propiedad soñada, dime tus prioridades actuales: ¿cuántos dorms, zona o presupuesto? Te muestro matches ideales.

¡Espero tu respuesta para ayudarte!

Responde STOP para optar-out.
    """,
    """
Buenas {cliente}, de PROCASA Jorge Pablo Caro Propiedades aquí. 😊 

Recordamos tu consulta por {prop_desc} con nuestro equipo. ¿Avanzaste con alguna visita? ¿Qué opinas?

Si continúas la búsqueda, cuéntame: ¿dorms, comuna, rango de precio? Tengo opciones frescas que pegan justo.

¡Hablemos para reconectar!

Responde STOP si prefieres no más notificaciones.
    """
]

MANUAL_NUMEROS = [
    #"+56983219804",  # Ejemplo: Pablo
    "+56940904971",  # Ejemplo: Jorge Pablo
    "+56940474465",  # Ejemplo: María Paz
    "+56961892120",  # Ejemplo: Raquel
    "+56991951317",  # Ejemplo: Erika
    "+56941829185",  # Ejemplo: Marcela
    "+56991788250",  # Ejemplo: Mariela
    "+56939125978",  # Ejemplo: Susana
]

ORDENES_COLLECTION = db["ordenes_visitas"]
STATS_COLLECTION = db["outreach_stats"]  # Nueva: Para contadores diarios

# ----------------------------
# FUNCIONES BASE OUTREACH
# ----------------------------

def normalize_phone(phone: str) -> str:
    """Normaliza formato de teléfono (quita + y espacios)."""
    if not phone:
        return ""
    phone = str(phone).replace(' ', '').replace('-', '')
    if phone.startswith('+'):
        phone = phone[1:]
    if len(phone) == 12 and phone.startswith('56'):
        return phone
    elif len(phone) == 11 and phone.startswith('569'):
        return phone
    return phone

def check_daily_limit() -> bool:
    """Chequea y actualiza límite diario global en DB."""
    today = datetime.now(timezone.utc).date().isoformat()
    doc = STATS_COLLECTION.find_one({"date": today})
    daily_sent = doc.get("sent_today", 0) if doc else 0
    
    if daily_sent >= MAX_MENSAJES_DIARIO:
        print(f"[ANTI-SPAM] Límite diario alcanzado: {daily_sent}/{MAX_MENSAJES_DIARIO}. Pausando envíos.")
        return False
    
    # Actualiza
    STATS_COLLECTION.update_one(
        {"date": today},
        {"$set": {"date": today, "$inc": {"sent_today": 1}}},
        upsert=True
    )
    print(f"[STATS] Envíos hoy: {daily_sent + 1}/{MAX_MENSAJES_DIARIO}")
    return True

def select_clients(limit: int = 10) -> list:
    """Selecciona clientes con telefono_valido=true, sin outreach previo."""
    total_docs = ORDENES_COLLECTION.count_documents({})
    print(f"[DEBUG] Total docs: {total_docs}")
    
    base_match = {
        "outreach_enviado": {"$exists": False},
        "telefono_valido": True,
        "telefono": {"$exists": True, "$ne": None, "$regex": "^(\\+?56)?[569]"}
    }
    
    pipeline = [
        {"$match": base_match},
        {"$sort": {"ingested_at": -1}},
        {"$limit": limit * 2},
        {"$addFields": {"first_detalle": {"$arrayElemAt": ["$ordenes_detalle", 0]}}},
        {"$project": {
            "telefono": 1,
            "cliente": 1,
            "codigo": "$first_detalle.Codigo",
            "operacion": "$first_detalle.Tipo Operación",
            "comuna": "$first_detalle.Comuna",
            "ingested_at": 1,
        }},
        {"$match": {"codigo": {"$exists": True}}},
    ]
    
    results = list(ORDENES_COLLECTION.aggregate(pipeline))
    print(f"[OUTREACH] Candidatos: {len(results)}")

    if not results:
        print("[ERROR] No se encontraron clientes válidos.")
    return results[:limit]

def send_outreach_message(telefono: str, cliente: str, codigo: str, operacion: str = "", comuna: str = "", is_manual: bool = False) -> bool:
    """Envía mensaje personalizado (template random) a través de Apichat (DB o manual)."""
    number = normalize_phone(telefono)
    if not number:
        print(f"[ERROR] Teléfono inválido: {telefono}")
        return False

    prop_desc = f"la propiedad Cód. {codigo} ({operacion} en {comuna})" if codigo and operacion and comuna else "la propiedad que te interesó"
    cliente_final = cliente

    # Elige template random
    template = random.choice(TEMPLATES)
    message = template.format(cliente=cliente_final, prop_desc=prop_desc).strip()

    # Chequeo de límite diario ANTES de enviar
    if not check_daily_limit():
        return False

    # Delay random entre envíos (human-like)
    delay = random.uniform(MIN_DELAY_BETWEEN_MSGS, MAX_DELAY_BETWEEN_MSGS)
    print(f"[DELAY] Esperando {delay:.1f}s antes de enviar a {telefono}...")
    time.sleep(delay)

    send_url = f"{config.APICHAT_BASE_URL}/sendText"
    send_data = {"number": number, "text": message}
    headers = {
        "client-id": str(config.APICHAT_CLIENT_ID),
        "token": config.APICHAT_TOKEN,
        "accept": "application/json",
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(send_url, json=send_data, headers=headers, timeout=config.APICHAT_TIMEOUT)
        if resp.status_code == 200:
            msg_id = resp.json().get('id', 'N/A')
            print(f"[OK ✅] Enviado a {telefono} ({cliente_final}) | ID: {msg_id} | Mensaje: {message[:50]}...")
            if not is_manual:
                # Actualiza DB solo para outreach automático
                ORDENES_COLLECTION.update_one(
                    {"telefono": telefono},
                    {"$set": {
                        "outreach_enviado": {
                            "mensaje": message,
                            "fecha": datetime.now(timezone.utc),
                            "exito": True,
                            "mensaje_id": msg_id
                        },
                        "ultima_interaccion": datetime.now(timezone.utc)
                    }}
                )
            return True
        else:
            print(f"[ERROR] Envío falló: {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        print(f"[ERROR] Excepción en envío: {e}")
        return False

# ========================================
# MODO 1: SOLO DB
# ========================================
def modo_db_only(max_mensajes: int = MAX_MENSAJES_DIARIO):
    print("=== Modo 1: Outreach Inicial SOLO a leads de DB ===")
    print(f"Límite diario: {MAX_MENSAJES_DIARIO}")
    print(f"Apichat Token OK: {'Sí' if config.APICHAT_TOKEN else 'NO'}")

    clients = select_clients(limit=max_mensajes)
    if not clients:
        print("[ERROR] No hay clientes para enviar.")
        return

    success_count = 0
    for c in clients:
        if send_outreach_message(
            c["telefono"],
            c.get("cliente", "Cliente"),
            c.get("codigo", "N/A"),
            c.get("operacion", ""),
            c.get("comuna", ""),
            is_manual=False
        ):
            success_count += 1

    print(f"\n=== RESUMEN MODO 1 ===")
    print(f"Enviados exitosos: {success_count}/{len(clients)}")

# ========================================
# MODO 3: SOLO MANUAL (Equipo/Jefes)
# ========================================
def modo_manual_only():
    print("\n=== Modo 3: ENVÍO SOLO A NÚMEROS MANUALES (Equipo/Jefes) ===")
    success_count = 0
    for numero in MANUAL_NUMEROS:
        nombre = "Equipo Procasa"
        if send_outreach_message(
            numero,
            nombre,
            "",  # No aplica para manual
            "",
            "",
            is_manual=True
        ):
            success_count += 1

    print(f"\n=== RESUMEN MODO 3 ===")
    print(f"Enviados exitosos: {success_count}/{len(MANUAL_NUMEROS)}")

# ========================================
# MODO 2: DB + MANUAL
# ========================================
def modo_db_plus_manual(max_mensajes: int = MAX_MENSAJES_DIARIO):
    print("=== Modo 2: Outreach Inicial a leads de DB + Números Manuales ===")
    print(f"Límite DB: {max_mensajes}")
    print(f"Apichat Token OK: {'Sí' if config.APICHAT_TOKEN else 'NO'}")

    # Primero, envía a DB
    clients = select_clients(limit=max_mensajes)
    db_success = 0
    if clients:
        for c in clients:
            if send_outreach_message(
                c["telefono"],
                c.get("cliente", "Cliente"),
                c.get("codigo", "N/A"),
                c.get("operacion", ""),
                c.get("comuna", ""),
                is_manual=False
            ):
                db_success += 1
    else:
        print("[INFO] No hay clientes DB para enviar.")

    # Luego, envía a manual
    manual_success = 0
    for numero in MANUAL_NUMEROS:
        nombre = "Equipo Procasa"
        if send_outreach_message(
            numero,
            nombre,
            "",  # No aplica
            "",
            "",
            is_manual=True
        ):
            manual_success += 1

    total_success = db_success + manual_success
    total_targets = len(clients) + len(MANUAL_NUMEROS)

    print(f"\n=== RESUMEN MODO 2 ===")
    print(f"DB exitosos: {db_success}/{len(clients)}")
    print(f"Manual exitosos: {manual_success}/{len(MANUAL_NUMEROS)}")
    print(f"Total exitosos: {total_success}/{total_targets}")

# ========================================
# MENÚ INTERACTIVO
# ========================================
def menu_interactivo():
    print("\n=== MENÚ DE MODO DE ENVÍO (v13 - Real, Sin Simulación) ===")
    print("1. Modo 1: SOLO DB (leads de base de datos)")
    print("2. Modo 2: DB + MANUAL (leads + números del equipo)")
    print("3. Modo 3: SOLO MANUAL (solo números del equipo/jefes)")
    print(f"Por defecto: {MAX_MENSAJES_DIARIO} mensajes/día. Delays: {MIN_DELAY_BETWEEN_MSGS}-{MAX_DELAY_BETWEEN_MSGS}s.")
    print("Nota: Templates random + footer opt-out. Envíos REALES.")
    
    while True:
        try:
            choice = input("\nElige el modo (1/2/3): ").strip()
            if choice in ['1', '2', '3']:
                break
            else:
                print("Opción inválida. Elige 1, 2 o 3.")
        except KeyboardInterrupt:
            print("\nSaliendo...")
            return
        
    # Preguntar límite para modos 1 y 2
    if choice in ['1', '2']:
        try:
            limit_input = input(f"Límite de mensajes DB (por defecto {MAX_MENSAJES_DIARIO}): ").strip()
            max_mensajes = int(limit_input) if limit_input.isdigit() else MAX_MENSAJES_DIARIO
        except ValueError:
            max_mensajes = MAX_MENSAJES_DIARIO
            print(f"Usando límite por defecto: {max_mensajes}")
    else:
        max_mensajes = 0  # No aplica

    # Confirmación final
    confirm_msg = f"¿Confirmar ejecución en Modo {choice}?"
    if choice in ['1', '2']:
        confirm_msg += f" (Límite DB: {max_mensajes})"
    confirm_msg += " | Envíos REALES (con delays anti-spam)"
    while True:
        confirm = input(f"\n{confirm_msg} (s/n): ").strip().lower()
        if confirm in ['s', 'si', 'y', 'yes']:
            break
        elif confirm in ['n', 'no']:
            print("Ejecución cancelada.")
            return
        else:
            print("Responde 's' para sí o 'n' para no.")

    # Ejecutar modo elegido
    if choice == '1':
        modo_db_only(max_mensajes=max_mensajes)
    elif choice == '2':
        modo_db_plus_manual(max_mensajes=max_mensajes)
    elif choice == '3':
        modo_manual_only()

# ========================================
# EJECUCIÓN PRINCIPAL
# ========================================
if __name__ == "__main__":
    print("=== Outreach Inicial: Envío a leads (v13 - Real, Sin Simulación) ===")
    menu_interactivo()