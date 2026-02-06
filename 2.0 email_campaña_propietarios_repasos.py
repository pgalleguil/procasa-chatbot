#!/usr/bin/env python3
import os
import smtplib
import time
import logging
import re
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
NOMBRE_CAMPANA = "ajuste_precio_202512_REPASO" # Nombre actualizado para el repaso
MODO_PRUEBA = True # CAMBIAR A False PARA ENVÍO MASIVO REAL
EMAIL_PRUEBA_DESTINO = "p.galleguil@gmail.com"
EMAIL_ADMIN = "jpcaro@procasa.cl"

RENDER_BASE_URL = "https://procasa-chatbot-yr8d.onrender.com"
WEBHOOK_PATH = "/campana/respuesta"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

client = MongoClient(Config.MONGO_URI)
db = client[Config.DB_NAME]
collection = db[Config.COLLECTION_CONTACTOS]

# ==============================================================================
# UTILIDADES
# ==============================================================================
def limpiar_telefono(tlf):
    if not tlf: return "56940904971"
    solo_numeros = re.sub(r'\D', '', str(tlf))
    if solo_numeros.startswith('569') and len(solo_numeros) == 11:
        return solo_numeros
    if solo_numeros.startswith('9') and len(solo_numeros) == 9:
        return f"56{solo_numeros}"
    return solo_numeros

def attach_images(msg):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    logo_path = os.path.join(base_dir, 'static', 'logo.png')
    if os.path.exists(logo_path):
        with open(logo_path, 'rb') as f:
            img = MIMEImage(f.read())
            img.add_header('Content-ID', '<logo_procasa>')
            msg.attach(img)

# ==============================================================================
# GENERACIÓN DE HTML
# ==============================================================================
def generar_html(nombre, propiedades, email_real, ejecutivo_nombre, ejecutivo_movil):
    filas = ""
    lista_codigos = [p.get('codigo', 'S/C') for p in propiedades]
    
    for p in propiedades:
        cod = p.get('codigo', 'S/C')
        link_p = f"https://www.procasa.cl/{cod}"
        filas += f"""
        <tr>
            <td style="padding: 15px; border-bottom: 1px solid #E2E8F0;">
                <a href="{link_p}" style="color: #004A99; font-weight: bold; text-decoration: underline; font-size: 14px;">{cod}</a>
            </td>
            <td style="padding: 15px; border-bottom: 1px solid #E2E8F0; font-size: 14px; color: #475569;">{p.get('tipo', 'Propiedad').title()}</td>
            <td style="padding: 15px; border-bottom: 1px solid #E2E8F0; text-align: right;">
                <span style="background: #FFF1F2; color: #E11D48; padding: 5px 10px; border-radius: 6px; font-size: 10px; font-weight: 800;">AJUSTAR VALOR</span>
            </td>
        </tr>"""

    email_encoded = quote(email_real)
    codigos_encoded = quote(", ".join(lista_codigos))
    
    # Enlaces de acción
    link_base = f"{RENDER_BASE_URL}{WEBHOOK_PATH}?email={email_encoded}&codigos={codigos_encoded}&campana={NOMBRE_CAMPANA}"
    link_ajuste   = f"{link_base}&accion=ajuste_7"
    link_mantener = f"{link_base}&accion=mantener"
    link_baja     = f"{link_base}&accion=no_disponible"
    
    # WhatsApp al ejecutivo
    whatsapp_num = limpiar_telefono(ejecutivo_movil)
    link_whatsapp = f"https://wa.me/{whatsapp_num}?text=Hola%20{quote(ejecutivo_nombre)},%20solicito%20asesoría%20para%20ajuste%20de%20precio%20de%20mi%20propiedad%20{codigos_encoded}"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            .btn-main:hover {{ background-color: #003366 !important; }}
            .btn-sec:hover {{ background-color: #F8FAFC !important; border-color: #004A99 !important; }}
        </style>
    </head>
    <body style="margin:0; padding:0; background-color:#F1F5F9; font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;">
        <table width="100%" border="0" cellspacing="0" cellpadding="0" style="padding: 20px;">
            <tr>
                <td align="center">
                    <table width="600" border="0" cellspacing="0" cellpadding="0" style="background-color:#ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 25px rgba(0,0,0,0.05);">
                        <tr>
                            <td align="center" style="padding: 40px 0 30px 0; border-bottom: 1px solid #F1F5F9;">
                                <img src="cid:logo_procasa" alt="Procasa" style="height: 98px; width: auto;">
                            </td>
                        </tr>
                        <tr>
                            <td style="padding: 40px 50px;">
                                <h2 style="color: #0F172A; font-size: 22px; margin: 0 0 20px 0;">Propuesta Estratégica de Mercado 2026: {nombre}</h2>
                                
                                <p style="color: #475569; font-size: 15px; line-height: 1.7;">
                                    Estimado cliente, iniciamos este año con un cambio estructural en el sector inmobiliario. La <strong>Tasa Hipotecaria ha bajado al 4.2%</strong>, una cifra significativamente más atractiva que el 5.3% que vimos en meses anteriores.
                                </p>
                                
                                <p style="color: #475569; font-size: 15px; line-height: 1.7;">
                                    <strong>¿Qué significa esto para usted?</strong> Que hoy existe un flujo real de compradores con créditos aprobados buscando invertir. Sin embargo, con un <b>stock récord de 67,000 propiedades</b> solo en la RM, los compradores tienen el poder de elegir. Las estadísticas son claras: las propiedades que no ajustan su valor al inicio del ciclo de baja de tasas tardan un 60% más en venderse y terminan cerrando por valores aún menores.
                                </p>

                                <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color: #0F172A; border-radius: 12px; margin: 25px 0; color: #ffffff;">
                                    <tr>
                                        <td style="padding: 20px;">
                                            <table width="100%" border="0" cellspacing="0" cellpadding="0">
                                                <tr>
                                                    <td align="center" width="33%">
                                                        <div style="font-size: 10px; color: #94A3B8; text-transform: uppercase;">Tasa Hipotecaria</div>
                                                        <div style="font-size: 20px; color: #ffffff; font-weight: bold;">4.2% <span style="font-size: 22px; color:#E11D48;">↓</span></div>
                                                    </td>
                                                    <td align="center" width="33%" style="border-left: 1px solid #334155; border-right: 1px solid #334155;">
                                                        <div style="font-size: 10px; color: #94A3B8; text-transform: uppercase;">Sobreoferta RM</div>
                                                        <div style="font-size: 20px; font-weight: bold;">+67k Unid.</div>
                                                    </td>
                                                    <td align="center" width="33%">
                                                        <div style="font-size: 10px; color: #94A3B8; text-transform: uppercase;">Plazo Venta</div>
                                                        <div style="font-size: 20px; font-weight: bold;">192 días</div>
                                                    </td>
                                                </tr>
                                            </table>
                                        </td>
                                    </tr>
                                </table>

                                <table width="100%" border="0" cellspacing="0" cellpadding="0" style="margin-bottom: 25px; border: 1px solid #E2E8F0; border-radius: 8px;">
                                    {filas}
                                </table>

                                <div style="background-color: #F0F9FF; border-left: 4px solid #004A99; padding: 15px; margin-bottom: 30px;">
                                    <p style="margin: 0; font-size: 14px; color: #0C4A6E;">
                                        <strong>Acción Procasa:</strong> Al aplicar el ajuste del 7%, activaremos un <strong>Posicionamiento de Élite</strong>. Su propiedad será destacada con algoritmos de "Oportunidad de Inversión", asegurando que aparezca en los primeros resultados para este nuevo flujo de compradores bancarizados.
                                    </p>
                                </div>

                                <a href="{link_ajuste}" class="btn-main" style="display: block; background-color: #004A99; color: #ffffff; padding: 18px; text-decoration: none; border-radius: 10px; font-weight: bold; font-size: 16px; text-align: center; margin-bottom: 15px;">
                                    SÍ, aplicar ajuste 7% y activar prioridad
                                </a>

                                <a href="{link_mantener}" class="btn-sec" style="display: block; text-align: center; border: 1px solid #CBD5E1; color: #475569; padding: 14px; text-decoration: none; border-radius: 10px; font-size: 14px; font-weight: 600;">Mantener precio actual</a>

                                <div style="text-align: center; margin-top: 25px;">
                                    <a href="{link_baja}" style="color: #94A3B8; font-size: 12px; text-decoration: underline;">Mi propiedad ya no está disponible</a>
                                </div>
                            </td>
                        </tr>
                        <tr>
                            <td style="background-color: #0F172A; padding: 40px; text-align: center;">
                                <p style="color: #ffffff; font-size: 15px; margin-bottom: 20px;">Contacto directo con su asesor inmobiliario: <strong>{ejecutivo_nombre}</strong></p>
                                <a href="{link_whatsapp}" style="background-color: #22C55E; color: #ffffff; padding: 12px 25px; text-decoration: none; border-radius: 50px; font-weight: bold; display: inline-block;">
                                    💬 Conversar por WhatsApp
                                </a>
                                <div style="margin-top: 35px; border-top: 1px solid #1E293B; padding-top: 20px; color: #64748B; font-size: 11px;">
                                    © 2026 Procasa Chile. Gestión estratégica de activos inmobiliarios.<br>
                                    <a href="{link_base}&accion=unsubscribe" style="color: #475569;">Darse de baja</a>
                                </div>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

# ==============================================================================
# ENVÍO Y LÓGICA DE COPIAS
# ==============================================================================
def enviar_correo(destinatario, asunto, html_body, email_ejecutivo):
    msg = MIMEMultipart('related')
    msg['From'] = f"Gestión Procasa <{Config.GMAIL_USER}>"
    msg['To'] = destinatario
    msg['Subject'] = asunto
    
    destinatarios_reales = [destinatario]
    
    if not MODO_PRUEBA:
        cc_list = [EMAIL_ADMIN]
        if email_ejecutivo and email_ejecutivo.lower() != EMAIL_ADMIN.lower():
            cc_list.append(email_ejecutivo.lower())
        msg['Cc'] = ", ".join(cc_list)
        destinatarios_reales += cc_list
    
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    attach_images(msg)
    
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
        server.sendmail(Config.GMAIL_USER, destinatarios_reales, msg.as_string())
        server.quit()
        return True, "Enviado"
    except Exception as e:
        return False, str(e)

# ==============================================================================
# MAIN
# ==============================================================================
def main():
    # Buscamos quienes no contestaron a la campaña original
    NOMBRE_ORIGINAL = "ajuste_precio_202512"

    pipeline = [
        { "$match": {
            "tipo": "propietario",
            "email_propietario": {"$exists": True, "$ne": "", "$ne": None},
            "estado_general": {"$ne": "baja_general"},
            "update_price.campana_nombre": NOMBRE_ORIGINAL,
            "estado": {"$exists": False} 
        }},
        { "$lookup": {
            "from": "universo_obelix",
            "localField": "codigo",
            "foreignField": "codigo",
            "as": "info"
        }},
        { "$unwind": { "path": "$info", "preserveNullAndEmptyArrays": True }},
        { "$match": {
            "$or": [
                {"info.region": "XIII Región Metropolitana"},
                {"info.region": "Región Metropolitana de Santiago"}
            ]
        }},
        { "$project": {
            "email_propietario": 1,
            "nombre_propietario": 1,
            "codigo": 1,
            "tipo": 1,
            "email_ejecutivo": "$info.email_ejecutivo",
            "ejecutivo": "$info.ejecutivo",
            "movil_ejecutivo": "$info.movil_ejecutivo"
        }}
    ]

    candidatos = collection.aggregate(pipeline)
    grouped_data = {}

    for doc in candidatos:
        email = doc.get("email_propietario", "").strip().lower()
        if "@" not in email: continue
            
        if email not in grouped_data:
            nombre_raw = doc.get("nombre_propietario", "")
            grouped_data[email] = {
                "nombre": nombre_raw.strip().split()[0].title() if nombre_raw else "Cliente",
                "propiedades": [],
                "ids": [],
                "ejecutivo_email": doc.get("email_ejecutivo"),
                "ejecutivo_nombre": doc.get("ejecutivo", "su Ejecutivo"),
                "ejecutivo_movil": doc.get("movil_ejecutivo")
            }
        grouped_data[email]["propiedades"].append({"codigo": doc["codigo"], "tipo": doc.get("tipo", "Propiedad")})
        grouped_data[email]["ids"].append(doc["_id"])

    print(f"Encontrados {len(grouped_data)} propietarios para re-envío.")

    enviados_prueba = 0
    for email_real, data in grouped_data.items():
        if MODO_PRUEBA and enviados_prueba >= 1:
            break
            
        destinatario_final = EMAIL_PRUEBA_DESTINO if MODO_PRUEBA else email_real
        asunto = f"RECORDATORIO: Decisión estratégica para su propiedad {data['propiedades'][0]['codigo']}"
        
        html = generar_html(data["nombre"], data["propiedades"], email_real, data["ejecutivo_nombre"], data["ejecutivo_movil"])
        exito, msg = enviar_correo(destinatario_final, asunto, html, data["ejecutivo_email"])

        if exito and (not MODO_PRUEBA or email_real.lower() == EMAIL_PRUEBA_DESTINO.lower()):
            collection.update_many(
                {"_id": {"$in": data["ids"]}},
                {"$set": {
                    "update_price.campana_nombre": NOMBRE_CAMPANA, # Se guarda como REPASO
                    "update_price.canales.email_reintento": {"enviado": True, "fecha": datetime.now(timezone.utc)}
                }}
            )
        
        enviados_prueba += 1
        log.info(f"Re-envío a {destinatario_final}: {msg}")
        if not MODO_PRUEBA: time.sleep(2)

if __name__ == "__main__":
    main()