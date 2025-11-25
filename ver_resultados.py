#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# INFORME FINAL + CÓDIGO DE PROPIEDAD – FUNCIONA 100 %

from pymongo import MongoClient
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

# CONEXIÓN A MONGO
client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("DB_NAME", "URLS")]
contactos = db["contactos"]        # ← ESTA LÍNEA FALTABA

# TODOS LOS DE LA CAMPAÑA 7%
enviados = list(contactos.find({
    "campanas.data_dura_7pct.enviado": True
}).sort("campanas.data_dura_7pct.fecha_envio", -1))

total = len(enviados)
aceptaron = []

for doc in enviados:
    c = doc.get("campanas", {}).get("data_dura_7pct", {})
    nombre = (doc.get("nombre_propietario") or "Sin nombre").split()[0].title()
    tel = c.get("telefono_usado", "N/A")
    version = c.get("version_mensaje", "v?")
    codigo_prop = doc.get("codigo", "SIN CÓDIGO")
    ultima_accion = doc.get("ultima_accion", "")
    autoriza = doc.get("autoriza_baja", False)

    # Última respuesta del cliente
    messages = doc.get("messages", [])
    ultima_respuesta = ""
    if messages and messages[-1]["role"] == "user":
        ultima_respuesta = messages[-1]["content"].lower()

    # Detectamos aceptación
    if (autoriza or 
        "baja_aceptada" in ultima_accion or 
        any(p in ultima_respuesta for p in ["ok", "dale", "sí", "si", "7%", "baja", "adelante", "hazlo", "390", "millones"])):
        aceptaron.append((nombre, tel, codigo_prop, version, ultima_respuesta or "OK implícito"))

# REPORTE FINAL
print("\n" + "═"*100)
print("       CAMPAÑA 7% – PROCASA – TENEMOS ACEPTACIONES")
print("═"*100)
print(f"Total enviados: {total} → Conversión directa: {len(aceptaron)/total:.1%}")
print("─"*100)

if aceptaron:
    print("ACEPTARON BAJAR EL 7% → LLAMAR YA + BAJAR PRECIO EN PORTAL:")
    for i, (n, t, cod, v, r) in enumerate(aceptaron, 1):
        print(f"  {i}. {n.ljust(15)} | {t} | CÓDIGO → {cod} | v{v}")
        print(f"{cod}")

else:
    print("Todavía no hay aceptaciones detectadas... pero siguen llegando!")

print("═"*100)
print("¡LLAMA YA A LOS QUE ACEPTARON Y CIERRAS 2 COMISIONES HOY!")
print("═"*100)