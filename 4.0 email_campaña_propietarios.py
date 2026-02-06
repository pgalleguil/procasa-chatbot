#!/usr/bin/env python3
# email_campana_propietarios_v7.py
# VERSIÓN 7.1 (FINAL - NOMBRE ASESOR DINÁMICO + FILTRO RM CORREGIDO)

import os
import smtplib
import time
import logging
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from urllib.parse import quote
from pymongo import MongoClient
from config import Config

# ==============================================================================
# CONFIGURACIÓN
# ==============================================================================
NOMBRE_CAMPANA = "ajuste_precio_202602_v4" 

CAMPANAS_ANTERIORES = [
    "ajuste_precio_202512", "ajuste_precio_regiones_202512",
    "ajuste_precio_202512_REPASO", "ajuste_precio_regiones_202512_REPASO",
    "ajuste_precio_202601_TERCER", "ajuste_precio_regiones_202601_TERCER",
    "ajuste_precio_202602_v4", "ajuste_precio_202602_v5_final", "ajuste_precio_202602_v6_final"
]

MODO_PRUEBA = False    
EMAIL_PRUEBA_DESTINO = "p.galleguil@gmail.com"
OFICINA_FILTRO = "INMOBILIARIA SUCRE SPA"

RENDER_BASE_URL = "https://procasa-chatbot-yr8d.onrender.com"
WEBHOOK_PATH = "/campana/respuesta"

# Teléfono centralizado para el botón de WhatsApp
TELEFONO_CENTRAL_WA = "56940904971"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

client = MongoClient(Config.MONGO_URI)
db = client[Config.DB_NAME]
collection = db[Config.COLLECTION_CONTACTOS]

# ==============================================================================
# IMÁGENES (SOLO LOGO)
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

# ==============================================================================
# HTML (FOOTER OSCURO + ASESOR DINÁMICO)
# ==============================================================================
def generar_html(nombre_propietario, propiedades, email_real, nombre_asesor):
    filas = ""
    lista_codigos = [p.get('codigo') for p in propiedades]
    codigos_str = ", ".join(lista_codigos)

    mensaje_wa = quote(f"Hola {nombre_asesor}, sobre mi propiedad {codigos_str}, prefiero conversar por aquí.")
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
                    En el mercado actual de Santiago, las unidades que no ajustan su valor
                    después de un periodo prolongado tienden a:
                </p>

                <ul style="font-size:14px;color:#374151;padding-left:18px;">
                    <li>Perder prioridad en portales y búsquedas</li>
                    <li>Ser descartadas rápidamente por compradores informados</li>
                    <li>Extender su tiempo en mercado sin generar nuevas señales de interés</li>
                </ul>

                <p>
                    Por esta razón, estamos recomendando a nuestros propietarios
                    <strong>un ajuste táctico y controlado de precio</strong>,
                    cuyo objetivo no es “regalar” la propiedad,
                    sino <strong>reactivar la demanda real</strong>
                    y volver a generar visitas calificadas.
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
                    para reposicionar la propiedad dentro del radar de compra,
                    sin comprometer el valor final de cierre.
                </p>

                <div style="text-align:center;margin:30px 0;">
                    <a href="{link_base}&accion=ajuste_7"
                       style="display:block;background:#2563EB;color:#FFFFFF;padding:16px;
                       text-decoration:none;border-radius:6px;font-weight:bold;font-size:16px;margin-bottom:15px;">
                        🚀 Aplicar ajuste estratégico y reactivar interés
                    </a>

                    <a href="{link_base}&accion=mantener"
                       style="display:block;background:#FFFFFF;color:#4B5563;padding:15px;
                       text-decoration:none;border-radius:6px;font-weight:600;font-size:15px;
                       border:2px solid #E5E7EB;">
                        Mantener precio actual (sin cambios)
                    </a>
                </div>

                <p style="font-size:13px;color:#9CA3AF;text-align:center;">
                    Si la propiedad ya no está disponible o desea una estrategia distinta,
                    puede informarnos directamente por WhatsApp.
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
# EJECUCIÓN
# ==============================================================================
def main():
    print(f"--- INICIANDO CAMPAÑA: {NOMBRE_CAMPANA} ---")
    
    pipeline = [
{
            "$match": {
                "tipo": "propietario",
                "update_price.campana_nombre": {"$in": CAMPANAS_ANTERIORES},
                # CAMBIO AQUÍ: Ahora acepta sin estado O con estado pendiente_llamada
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
                # 1. Filtro de disponibilidad y oficina
                "info.disponible": True,
                "info.oficina": OFICINA_FILTRO,
                
                # 2. FILTRO REGIÓN METROPOLITANA
                "$or": [
                    {"info.region": "XIII Región Metropolitana"},
                    {"info.region": "Región Metropolitana de Santiago"},
                    {"info.region": {"$regex": "Metropolitana", "$options": "i"}}
                ]
            }
        }
    ]

    candidatos = list(collection.aggregate(pipeline))
    grouped_data = {}

    for doc in candidatos:
        email = doc.get("email_propietario", "").strip().lower()
        if "@" not in email: continue
        
        # EXTRACCIÓN DINÁMICA DEL ASESOR/CAPTADOR
        info_bd = doc.get('info', {})
        asesor_bd = info_bd.get('captador') or info_bd.get('nombre_captador') or "Asesor Procasa"
        asesor_bd = asesor_bd.strip().title()

        if email not in grouped_data:
            nombre_raw = doc.get("nombre_propietario", "").strip()
            nombre_prop = nombre_raw.split()[0].title() if nombre_raw else "Propietario"
            
            grouped_data[email] = {
                "nombre": nombre_prop, 
                "propiedades": [], 
                "ids": [], 
                "asesor": asesor_bd 
            }
        
        grouped_data[email]["propiedades"].append({
            "codigo": doc.get("codigo"),
            "tipo": doc.get("tipo", "Propiedad")
        })
        grouped_data[email]["ids"].append(doc["_id"])

    print(f"Total candidatos (Oficina Sucre + RM): {len(grouped_data)}")

    if not MODO_PRUEBA:
        if input("Escriba 'ENVIAR' para proceder: ") != "ENVIAR": return

    enviados = 0
    for email_real, data in grouped_data.items():
        if MODO_PRUEBA and enviados >= 1: break

        destinatario = EMAIL_PRUEBA_DESTINO if MODO_PRUEBA else email_real
        nombre_asesor_actual = data['asesor']
        
        codigo_principal = data['propiedades'][0]['codigo']
        asunto = f"Seguimiento mensual: Posicionamiento de precio propiedad {codigo_principal}"
        
        html = generar_html(data["nombre"], data["propiedades"], email_real, nombre_asesor_actual)
        
        msg = MIMEMultipart('related')
        msg['From'] = f"{nombre_asesor_actual} - Procasa <{Config.GMAIL_USER}>"
        msg['To'] = destinatario
        msg['Subject'] = asunto
        msg.attach(MIMEText(html, 'html', 'utf-8'))
        
        attach_images(msg)

        try:
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
            server.send_message(msg)
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
                time.sleep(1.5)
        except Exception as e:
            log.error(f"Error con {email_real}: {e}")

    print(f"Finalizado. Enviados: {enviados}")

if __name__ == "__main__":
    main()