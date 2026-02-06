#!/usr/bin/env python3
# email_campana_propietarios_v7_REGIONES_FINAL.py
# VERSIÓN 7.4 (CORREGIDA: BÚSQUEDA EJECUTIVO PRIORIZADA + DELAY ANTI-BLOQUEO)
#CONSISERAR QUE ESTA VERSION COPIA JORGE Y AL EJECUTIVO SOLO CUANDO MODO_PRUEBA = False y etsa agragado la opcion cuando un propietario informa que la propiedad no esta disponible
#PORQUE EN RM NO ESTA INCPPORADO SE DEBO MODIFICAR PARA EL PROX ENVIO

import os
import smtplib
import time
import logging
import random
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from urllib.parse import quote
from pymongo import MongoClient
from config import Config

# ==============================================================================
# CONFIGURACIÓN GENERAL DE LA CAMPAÑA
# ==============================================================================
NOMBRE_CAMPANA = "ajuste_precio_regiones_202602_v4" 

CAMPANAS_ANTERIORES = [
    "ajuste_precio_202512", "ajuste_precio_regiones_202512",
    "ajuste_precio_202512_REPASO", "ajuste_precio_regiones_202512_REPASO",
    "ajuste_precio_202601_TERCER", "ajuste_precio_regiones_202601_TERCER",
    "ajuste_precio_202602_v4"
]

MODO_PRUEBA = False    
EMAIL_PRUEBA_DESTINO = "p.galleguil@gmail.com"
EMAIL_JEFE = "jpcaro@procasa.cl"
OFICINA_FILTRO = "INMOBILIARIA SUCRE SPA"

# Límite de seguridad para evitar bloqueo de Gmail
MAX_ENVIOS_POR_EJECUCION = 200

RENDER_BASE_URL = "https://procasa-chatbot-yr8d.onrender.com"
WEBHOOK_PATH = "/campana/respuesta"

TELEFONO_CENTRAL_WA = "56940904971"

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

client = MongoClient(Config.MONGO_URI)
db = client[Config.DB_NAME]
collection = db[Config.COLLECTION_CONTACTOS]

# ==============================================================================
# MANEJO DE IMÁGENES
# ==============================================================================
def attach_images(msg):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    img_path = os.path.join(base_dir, 'static', 'logo.png')
    
    if not os.path.exists(img_path):
        img_path = os.path.join(base_dir, 'logo.png')

    if os.path.exists(img_path):
        with open(img_path, 'rb') as f:
            img = MIMEImage(f.read())
            img.add_header('Content-ID', '<logo_procasa>')
            msg.attach(img)
    else:
        log.warning("No se encontró el archivo logo.png en static/ o raíz.")

# ==============================================================================
# GENERACIÓN DE HTML
# ==============================================================================
def generar_html(nombre_propietario, propiedades, email_real, nombre_asesor):
    filas = ""
    lista_codigos = [p.get('codigo') for p in propiedades]
    codigos_str = ", ".join(lista_codigos)

    mensaje_wa = quote(f"Hola {nombre_asesor}, sobre mi propiedad {codigos_str} en regiones, prefiero conversar por aquí.")
    link_whatsapp = f"https://wa.me/{TELEFONO_CENTRAL_WA}?text={mensaje_wa}"

    for p in propiedades:
        cod = p.get('codigo', 'S/C')
        filas += f"""
        <tr>
            <td style="padding:12px 15px;border-bottom:1px solid #E2E8F0;font-weight:600;">
                <a href="https://www.procasa.cl/{cod}" target="_blank" style="color:#2563EB;text-decoration:none;">{cod}</a>
            </td>
            <td style="padding:12px 15px;border-bottom:1px solid #E2E8F0;color:#64748B;">
                {p.get('tipo','Propiedad').title()}
            </td>
            <td style="padding:12px 15px;border-bottom:1px solid #E2E8F0;color:#DC2626;font-weight:bold;font-size:12px;">
                Sin ofertas activas
            </td>
        </tr>
        """

    email_encoded = quote(email_real)
    codigos_encoded = quote(codigos_str)
    link_base = f"{RENDER_BASE_URL}{WEBHOOK_PATH}?email={email_encoded}&codigos={codigos_encoded}&campana={NOMBRE_CAMPANA}"
    link_ajuste = f"{link_base}&accion=ajuste_7"
    link_mantener = f"{link_base}&accion=mantener"
    link_no_disponible = f"{link_base}&accion=no_disponible"

    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head><meta charset="UTF-8"></head>
    <body style="margin:0;padding:0;background:#F3F4F6;font-family:Arial,sans-serif;">
        <div style="max-width:600px;margin:0 auto;background:#FFFFFF;border-radius:8px;
                    overflow:hidden;box-shadow:0 4px 10px rgba(0,0,0,.1);">

            <div style="padding:20px 24px;text-align:center;border-bottom:1px solid #F3F4F6;">
                <img src="cid:logo_procasa" width="110" alt="Procasa">
            </div>

            <div style="padding:28px 32px;color:#374151;line-height:1.6;">

                <p style="font-size:16px;margin:0 0 12px 0;">Hola <strong>{nombre_propietario}</strong>,</p>
                
                <p>
                    Le escribe <strong>{nombre_asesor}</strong>. 
                    Estamos realizando un cierre de control de cartera y su propiedad
                    (<strong>{codigos_str}</strong>) continúa publicada sin ofertas activas ni negociaciones en curso.
                </p>

                <p>
                    A esta altura del proceso, el factor más relevante ya no es la exposición,
                    sino <strong>la posición de precio frente a propiedades comparables</strong>.
                    En los mercados regionales, las unidades que no ajustan su valor
                    tienden a perder prioridad en portales y ser descartadas por compradores informados.
                </p>

                <p>
                    Por esta razón, estamos recomendando a nuestros propietarios fuera de la RM
                    <strong>un ajuste táctico y controlado de precio</strong>,
                    cuyo objetivo no es “regalar” la propiedad, sino <strong>reactivar la demanda real</strong>
                    y volver a generar visitas calificadas en su zona.
                </p>

                <table width="100%" style="border-collapse:collapse;margin:20px 0;font-size:14px;">
                    <thead style="background:#F9FAFB;">
                        <tr>
                            <th style="padding:10px 15px;text-align:left;color:#6B7280;">Código</th>
                            <th style="padding:10px 15px;text-align:left;color:#6B7280;">Tipo</th>
                            <th style="padding:10px 15px;text-align:left;color:#6B7280;">Estado actual</th>
                        </tr>
                    </thead>
                    <tbody>{filas}</tbody>
                </table>

                <p style="font-size:14px;">
                    📌 <strong>Nota operativa:</strong><br>
                    Ajustes en el rango del <strong>5% al 7%</strong> suelen ser suficientes
                    para reposicionar la propiedad dentro del radar de compra local.
                </p>

                <div style="text-align:center;margin:30px 0;">
                    <a href="{link_ajuste}"
                       style="display:block;background:#2563EB;color:#FFFFFF;padding:16px;
                       text-decoration:none;border-radius:6px;font-weight:bold;font-size:16px;margin-bottom:15px;">
                        🚀 Aplicar ajuste estratégico y reactivar interés
                    </a>

                    <a href="{link_mantener}"
                       style="display:block;background:#FFFFFF;color:#4B5563;padding:15px;
                       text-decoration:none;border-radius:6px;font-weight:600;font-size:15px;
                       border:2px solid #E5E7EB;">
                        Mantener precio actual (sin cambios)
                    </a>
                </div>

                <div style="text-align:center;margin:25px 0;">
                    <a href="{link_no_disponible}" style="color:#94A3AF;font-size:13px;text-decoration:underline;">
                        Mi propiedad ya no está disponible
                    </a>
                </div>

                <p style="font-size:13px;color:#9CA3AF;text-align:center;">
                    Si desea una estrategia distinta, puede informarnos directamente por WhatsApp.
                </p>
            </div>

            <div style="background:#0F172A;padding:28px 20px;text-align:center;color:#FFFFFF;">

                <p style="font-size:18px;margin-bottom:20px;">
                    Su asesor asignado<br>
                    <strong>{nombre_asesor}</strong>
                </p>

                <a href="{link_whatsapp}" target="_blank"
                   style="display:inline-block;background:#22C55E;color:#FFFFFF;
                   text-decoration:none;padding:14px 28px;border-radius:50px;
                   font-weight:bold;font-size:16px;">
                    💬 Conversar por WhatsApp
                </a>

                <div style="border-top:1px solid #1E293B;margin:20px auto;width:70%;"></div>

                <p style="font-size:12px;color:#64748B;">© 2026 Procasa Chile · Oficina Sucre</p>
                <p>
                    <a href="{link_base}&accion=baja"
                       style="color:#64748B;font-size:12px;text-decoration:underline;">
                        Darse de baja
                    </a>
                </p>
            </div>
        </div>
    </body>
    </html>
    """
    return html

# ==============================================================================
# PROCESAMIENTO Y ENVÍO
# ==============================================================================
def main():
    print(f"--- INICIANDO CAMPAÑA REGIONES: {NOMBRE_CAMPANA} ---")
    
    pipeline = [
        {
            "$match": {
                "tipo": "propietario",
                "update_price.campana_nombre": {"$in": CAMPANAS_ANTERIORES},
                "$or": [
                    {"estado": {"$exists": False}},
                    {"estado": "pendiente_llamada"}
                ]
            }
        },
        {
            "$lookup": {
                "from": "universo_obelix",
                "localField": "codigo",
                "foreignField": "codigo",
                "as": "info"
            }
        },
        { "$unwind": "$info" },
        {
            "$match": {
                "info.disponible": True,
                "info.oficina": OFICINA_FILTRO,
                "info.region": {
                    "$nin": [
                        "XIII Región Metropolitana",
                        "Región Metropolitana de Santiago"
                    ],
                    "$not": {"$regex": "Metropolitana", "$options": "i"}
                }
            }
        }
    ]

    candidatos = list(collection.aggregate(pipeline))
    grouped_data = {}

    for doc in candidatos:
        email = doc.get("email_propietario", "").strip().lower()
        if "@" not in email: continue
        
        info_bd = doc.get('info', {})
        
        # Prioridad exacta como en el código anterior
        nombre_asesor = (info_bd.get('ejecutivo') or 
                        info_bd.get('captador') or 
                        info_bd.get('nombre_captador') or 
                        "Asesor Procasa").strip().title()
        
        email_asesor = (info_bd.get('email_ejecutivo') or 
                       info_bd.get('email_captador') or "").strip().lower()

        if email not in grouped_data:
            nombre_raw = doc.get("nombre_propietario", "").strip()
            nombre_prop = nombre_raw.split()[0].title() if nombre_raw else "Propietario"
            
            grouped_data[email] = {
                "nombre": nombre_prop, 
                "propiedades": [], 
                "ids": [], 
                "asesor": nombre_asesor,
                "email_asesor": email_asesor if "@" in email_asesor else None
            }
        
        grouped_data[email]["propiedades"].append({
            "codigo": doc.get("codigo"),
            "tipo": doc.get("tipo", "Propiedad")
        })
        grouped_data[email]["ids"].append(doc["_id"])

    print(f"Total candidatos detectados en REGIONES: {len(grouped_data)}")

    if not MODO_PRUEBA and len(grouped_data) > 0:
        if input("Escriba 'ENVIAR' para proceder con el envío masivo: ") != "ENVIAR": 
            print("Envío cancelado.")
            return

    enviados = 0
    for email_real, data in grouped_data.items():
        if MODO_PRUEBA and enviados >= 1: 
            break
            
        if enviados >= MAX_ENVIOS_POR_EJECUCION:
            print(f"Límite de {MAX_ENVIOS_POR_EJECUCION} envíos alcanzado esta ejecución. Vuelve a correr el script para continuar.")
            break

        destinatario = EMAIL_PRUEBA_DESTINO if MODO_PRUEBA else email_real
        nombre_asesor_actual = data['asesor']
        
        codigo_principal = data['propiedades'][0]['codigo']
        asunto = f"Seguimiento mensual: Posicionamiento de precio propiedad {codigo_principal}"
        
        html = generar_html(data["nombre"], data["propiedades"], email_real, nombre_asesor_actual)
        
        msg = MIMEMultipart('related')
        msg['From'] = f"{nombre_asesor_actual} - Procasa <{Config.GMAIL_USER}>"
        msg['To'] = destinatario
        msg['Subject'] = asunto

        destinatarios_finales = [destinatario]
        
        if not MODO_PRUEBA:
            msg['Bcc'] = EMAIL_JEFE
            destinatarios_finales.append(EMAIL_JEFE)
            
            if data.get('email_asesor') and data['email_asesor'] != EMAIL_JEFE:
                msg['Cc'] = data['email_asesor']
        destinatarios_finales.append(data['email_asesor'])

        msg.attach(MIMEText(html, 'html', 'utf-8'))
        attach_images(msg)

        try:
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
            server.send_message(msg, to_addrs=destinatarios_finales)
            server.quit()
            
            if not MODO_PRUEBA:
                ahora = datetime.now(timezone.utc)
                collection.update_many(
                    {"_id": {"$in": data["ids"]}},
                    {"$set": {
                        "update_price.campana_nombre": NOMBRE_CAMPANA,
                        "update_price.ultima_actualizacion": ahora,
                        "update_price.canales.email_v4": {"enviado": True, "fecha": ahora}
                    }}
                )
            log.info(f"Enviado a: {email_real} (Asesor: {nombre_asesor_actual})")
            enviados += 1
            
            if not MODO_PRUEBA:
                delay = random.uniform(5, 10)
                log.info(f"Esperando {delay:.1f} segundos antes del siguiente...")
                time.sleep(delay)
                
        except Exception as e:
            log.error(f"Error enviando a {email_real}: {e}")

    print(f"--- PROCESO FINALIZADO ---")
    print(f"Total correos enviados esta ejecución: {enviados}")

if __name__ == "__main__":
    main()