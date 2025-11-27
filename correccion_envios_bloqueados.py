#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# LIMPIEZA DEFINITIVA Y REUTILIZABLE – INTENTOS FALLIDOS POR BLOQUEO META
# Usa este script CADA VEZ que Meta te bloquee y tengas que limpiar fantasmas

from pymongo import MongoClient
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("DB_NAME", "URLS")]
contactos = db["contactos"]

# === AQUÍ PONES LOS TELÉFONOS QUE CONFIRMASTE QUE NO LLEGARON ===
# (Puedes copiar-pegar directo de WhatsApp o del txt)
fantasmas = [
    "+56963605521", "+56981656902", "+56998833382", "+56940223358", "+56977682282",
    "+56985498668", "+56989295774", "+56998259984", "+56992181099", "+56959075805",
    "+56977798670", "+56963278358", "+56989202404", "+56995577085", "+56990896518",
    "+56979662774", "+56939492617", "+56942305182", "+56963201171", "+56971783079",
    "+56977963239", "+56972439196", "+56971067269", "+56986458401"
    # ← Si mañana pasa de nuevo, solo pegas aquí los nuevos
]

print(f"Limpiando {len(fantasmas)} intentos fallidos por bloqueo de Meta...\n")

for tel in fantasmas:
    # Buscamos el documento para recuperar la fecha original del intento
    doc = contactos.find_one({"campanas.data_dura_7pct.telefono_usado": tel})
    fecha_original = None
    if doc:
        campana = doc.get("campanas", {}).get("data_dura_7pct", {})
        fecha_original = campana.get("fecha_envio") or campana.get("fecha_intento_fallido")

    result = contactos.update_one(
        {"campanas.data_dura_7pct.telefono_usado": tel},
        {
            "$set": {
                "campanas.data_dura_7pct.enviado": False,
                "campanas.data_dura_7pct.intento_fallido": True,
                "campanas.data_dura_7pct.fecha_intento": fecha_original,   # ← fecha REAL del intento
                "campanas.data_dura_7pct.motivo": "bloqueo_meta",          # ← corto y claro
                "estado": None                                              # ← sin estado (nunca llegó)
            },
            "$unset": {
                "campanas.data_dura_7pct.fecha_envio": "",
                "campanas.data_dura_7pct.msg_id_wasender": "",
                "campanas.data_dura_7pct.version_mensaje": "",
                "campanas.data_dura_7pct.bloqueado_meta": "",
                "campanas.data_dura_7pct.motivo_fallo": "",
                "campanas.data_dura_7pct.fecha_intento_fallido": "",
                "campanas.data_dura_7pct.limpiado_manual": ""
            }
        }
    )
    fecha_mostrar = fecha_original.strftime("%d-%m-%Y %H:%M") if fecha_original else "sin fecha"
    print(f"{'CORREGIDO' if result.modified_count else 'YA OK'} → {tel} | Intento: {fecha_mostrar}")

print("\n¡100% PERFECTO!")
print("   • motivo: bloqueo_meta")
print("   • fecha_intento: la fecha REAL del intento original (no inventada)")
print("   • estado: null → porque nunca llegó el mensaje")
print("   • Script reutilizable: solo cambias la lista y lo ejecutas de nuevo")
print("   • Histórico limpio y honesto")