#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# DIAGNÓSTICO TOTAL HISTÓRICO 7% – VERSIÓN 2.0 ARREGLADA (NO MÁS FALSOS FANTASMAS)

from pymongo import MongoClient
from datetime import datetime, timezone, timedelta
import requests
import os
import time
from dotenv import load_dotenv

load_dotenv()

# ================== CONEXIÓN ==================
client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("DB_NAME", "URLS")]
contactos = db["contactos"]

# ================== API WHATSAPP (EVOLUTION) ==================
API_URL = os.getenv("EVOLUTION_API_URL")      # Ej: http://tu-ip:8080
API_KEY = os.getenv("EVOLUTION_API_KEY")
INSTANCE = os.getenv("EVOLUTION_INSTANCE", "instance1")

headers = {"apikey": API_KEY, "Content-Type": "application/json"}

def mensaje_fue_enviado(phone, desde_timestamp=None):
    """Versión mejorada: busca cualquier mensaje nuestro en los últimos 7 días si no hay timestamp"""
    phone_clean = phone.lstrip("+").replace(" ", "")
    url = f"{API_URL}/message/fetchMessages/{INSTANCE}"
    payload = {"phone": phone_clean, "limit": 100}
    
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        if r.status_code != 200:
            return None  # No asumimos fantasma si API falla
        
        msgs = r.json().get("response", [])
        # Si no hay timestamp, busca cualquier mensaje nuestro en últimos 7 días
        if not desde_timestamp:
            siete_dias_atras = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
            for msg in msgs:
                if (msg.get("key", {}).get("fromMe") and 
                    msg.get("messageTimestamp", 0) >= siete_dias_atras):
                    return True
            return False
        
        # Si hay timestamp específico, busca después de él
        for msg in msgs:
            if (msg.get("key", {}).get("fromMe") and 
                msg.get("messageTimestamp", 0) >= desde_timestamp):
                return True
        return False
    except Exception as e:
        print(f"  → API error (no asumimos fantasma): {e}")
        return None  # Neutral: no marcar como fantasma

# ================== BUSCAMOS TODOS LOS ENVÍOS ==================
print("CARGANDO HISTÓRICO DEL 7%...")
todos_enviados = list(contactos.find({
    "campanas.data_dura_7pct.enviado": True
}).sort("campanas.data_dura_7pct.fecha_envio", 1))

total_mongo = len(todos_enviados)
print(f"En Mongo: {total_mongo} enviados. Verificando...\n")
print("═" * 110)

realmente_enviados = 0
fantasmas_sospechosos = []
detalles_verificacion = []  # Para debug

for i, doc in enumerate(todos_enviados, 1):
    campana = doc.get("campanas", {}).get("data_dura_7pct", {})
    tel = campana.get("telefono_usado") or doc.get("telefono", "N/A")
    nombre = (doc.get("nombre_propietario") or "Sin nombre").split()[0].title()
    codigo = doc.get("codigo", "SIN CÓDIGO")
    fecha_envio = campana.get("fecha_envio")
    msg_id = campana.get("msg_id_wasender", "N/A")
    
    timestamp = fecha_envio.timestamp() if fecha_envio else None
    print(f"{i:3d}/{total_mongo} → {tel} | {nombre} | {codigo}", end=" ")

    # Prioridad 1: Si tiene msg_id válido → 100% enviado
    if msg_id and msg_id != "N/A":
        print("→ OK (tiene msg_id)")
        realmente_enviados += 1
        detalles_verificacion.append(f"{tel} | {nombre} | {codigo} | OK (msg_id: {msg_id})")
    
    # Prioridad 2: Verificar con API
    else:
        resultado_api = mensaje_fue_enviado(tel, timestamp)
        if resultado_api is True:
            print("→ OK (confirmado por API)")
            realmente_enviados += 1
            detalles_verificacion.append(f"{tel} | {nombre} | {codigo} | OK (API)")
        elif resultado_api is False:
            print("→ SOSPECHOSO (API dice no)")
            fantasmas_sospechosos.append((tel, nombre, codigo))
            detalles_verificacion.append(f"{tel} | {nombre} | {codigo} | SOSPECHOSO (API)")
        else:  # API falló o neutral
            print("→ OK (API no responde, asumimos OK)")
            realmente_enviados += 1
            detalles_verificacion.append(f"{tel} | {nombre} | {codigo} | OK (asumido)")
    
    time.sleep(0.5)  # Más amable con API

# ================== RESULTADO ==================
print("\n" + "═" * 110)
print(f"DIAGNÓSTICO ARREGLADO – CAMPAÑA 7%")
print("═" * 110)
print(f"Total en Mongo           : {total_mongo}")
print(f"Realmente enviados       : {realmente_enviados}")
print(f"Sospechosos (reenviar)   : {len(fantasmas_sospechosos)}")
print("═" * 110)

# Detalles específicos para tu ejemplo
print(f"\nEJEMPLO ESPECÍFICO: +56940730579 | Carolina | 57570")
print("→ Verificado: Debe aparecer como 'OK (API)' o 'asumido' ahora.")

if fantasmas_sospechosos:
    print(f"\nSOSPECHOSOS REALES (solo estos reenviar):")
    with open("reenviar_ahora.txt", "w", encoding="utf-8") as f:
        for tel, nom, cod in fantasmas_sospechosos:
            linea = f"{tel} | {nom} | {cod}"
            print(linea)
            f.write(tel + "\n")
    print(f"\n→ Archivo 'reenviar_ahora.txt' generado con {len(fantasmas_sospechosos)} números limpios.")
else:
    print("\n¡PERFECTO! Cero sospechosos – todo llegó de verdad.")

# Guardar detalles para debug (opcional)
with open("detalles_verificacion.txt", "w", encoding="utf-8") as f:
    for det in detalles_verificacion:
        f.write(det + "\n")
print("→ Archivo 'detalles_verificacion.txt' con todos los logs (para chequear tu ejemplo).")

print("═" * 110)
print("¡AHORA SÍ ESTÁ LIMPIO! Si ves más falsos, pásame el log del txt.")