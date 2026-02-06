#!/usr/bin/env python3
# email_campaña_propietarios.py → VERSIÓN FINAL DEFINITIVA (TODOS LOS CÓDIGOS CLICABLES)

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
NOMBRE_CAMPANA = "ajuste_precio_202512"
MODO_PRUEBA = False                    # ← Cambia a False solo cuando envíes de verdad
EMAIL_PRUEBA_DESTINO = "p.galleguil@gmail.com"

RENDER_BASE_URL = "https://procasa-chatbot-yr8d.onrender.com"
WEBHOOK_PATH = "/campana/respuesta"
CODIGOS_EXCLUIDOS = ["12345", "PRO-999"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

client = MongoClient(Config.MONGO_URI)
db = client[Config.DB_NAME]
collection = db[Config.COLLECTION_CONTACTOS]

# ==============================================================================
# ADJUNTA LOGOS
# ==============================================================================
def attach_images(msg):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for p in [os.path.join(base_dir, 'static', 'logo.png'),
              os.path.join(base_dir, '..', 'static', 'logo.png'),
              os.path.join(base_dir, 'logo.png')]:
        if os.path.exists(p):
            with open(p, 'rb') as f:
                img = MIMEImage(f.read())
                img.add_header('Content-ID', '<logo_procasa>')
                msg.attach(img)
            break
    for p in [os.path.join(base_dir, 'static', 'whatsapp.png'),
              os.path.join(base_dir, '..', 'static', 'whatsapp.png'),
              os.path.join(base_dir, 'whatsapp.png')]:
        if os.path.exists(p):
            with open(p, 'rb') as f:
                img = MIMEImage(f.read())
                img.add_header('Content-ID', '<whatsapp_icon>')
                msg.attach(img)
            break

# ==============================================================================
# GENERACIÓN DE HTML (TODOS los códigos son hipervínculos azules)
# ==============================================================================
def generar_html(nombre, propiedades, email_real):
    filas = ""
    lista_codigos = []
    codigos_con_link = []        # ← Para el texto del primer párrafo

    for p in propiedades:
        cod = p.get('codigo', 'S/C')
        lista_codigos.append(cod)
        codigos_con_link.append(f'<a href="https://www.procasa.cl/{cod}" style="color:#0066CC; font-weight:600; text-decoration:underline;" target="_blank">{cod}</a>')
        
        filas += f"""
        <tr>
            <td style="padding: 12px 16px; border-bottom: 1px solid #E2E8F0; font-weight: 600; font-size: 15px;">
                <a href="https://www.procasa.cl/{cod}" style="color: #0066CC; text-decoration: underline;" target="_blank">{cod}</a>
            </td>
            <td style="padding: 12px 16px; border-bottom: 1px solid #E2E8F0; color: #64748B; font-size: 15px;">{p.get('tipo', 'Propiedad').title()}</td>
            <td style="padding: 12px 16px; border-bottom: 1px solid #E2E8F0; color: #F59E0B; font-weight: 600; font-size: 13px;">Revisión sugerida</td>
        </tr>
        """

    codigos_str = ", ".join(lista_codigos)
    codigos_html = ", ".join(codigos_con_link)   # ← Versión con hipervínculos
    es_plural = len(propiedades) > 1
    texto_prop = "sus propiedades" if es_plural else "su propiedad"

    email_encoded = quote(email_real)
    codigos_encoded = quote(codigos_str)
    link_base = f"{RENDER_BASE_URL}{WEBHOOK_PATH}?email={email_encoded}&codigos={codigos_encoded}&campana={NOMBRE_CAMPANA}"

    link_ajuste   = f"{link_base}&accion=ajuste_7"
    link_mantener = f"{link_base}&accion=mantener"
    link_baja     = f"{link_base}&accion=no_disponible"
    link_llamada  = f"{link_base}&accion=llamada"
    link_unsubscribe = f"{link_base}&accion=unsubscribe"

    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ margin:0; padding:0; background:#F8FAFC; font-family:'Segoe UI',Helvetica,Arial,sans-serif; }}
            .wrapper {{ max-width:600px; margin:0 auto; background:#F8FAFC; padding:20px 0; }}
            .card {{ background:#FFFFFF; border-radius:16px; overflow:hidden; box-shadow:0 10px 30px rgba(0,0,0,0.08); border:1px solid #E2E8F0; }}
            .header {{ padding:40px 40px 10px; text-align:center; }}
            .header img {{ height:98px; width:auto; }}
            .content {{ padding:20px 40px 40px; line-height:1.65; font-size:15.5px; color:#334155; }}
            strong {{ color:#0F172A; }}
            .highlight-box {{ background:#F0F9FF; border-left:5px solid #0EA5E9; padding:20px; margin:28px 0; border-radius:0 8px 8px 0; font-size:14.5px; }}
            table {{ width:100%; border-collapse:collapse; margin:32px 0; background:#FAFAFA; border-radius:10px; overflow:hidden; }}
            th {{ background:#E0F2FE; color:#0C4A6E; padding:14px 16px; text-align:left; font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:0.8px; }}
            .btn-container {{ text-align:center; margin:40px 0 20px; }}
            .btn {{ display:block; max-width:420px; margin:0 auto 14px; padding:18px 24px; border-radius:12px; font-weight:700; font-size:16px; text-decoration:none; text-align:center; transition:all 0.2s; box-shadow:0 4px 15px rgba(0,0,0,0.1); }}
            .btn-ajuste   {{ background:#0066CC; color:#FFFFFF !important; }}
            .btn-ajuste:hover {{ background:#0052A3; }}
            .btn-otros    {{ background:#E0F2FE; color:#0C4A6E; border:1px solid #99F6E4; }}
            .btn-otros:hover {{ background:#BAE6FD; }}
            .footer {{ background:#F1F5F9; padding:30px 40px; text-align:center; font-size:12px; color:#647588; border-top:1px solid #E2E8F0; }}
            .footer a {{ color:#475569; text-decoration:underline; transition:color 0.2s; }}
            .footer a:hover {{ color:#0C4A6E; }}
            .telefono {{ font-size:17px; font-weight:700; color:#0C4A6E; margin:8px 0; display:block; }}
            .wa-text {{ color:#25D366; font-weight:600; transition:color 0.2s; }}
            .wa-text:hover {{ color:#128C7E; }}
        </style>
    </head>
    <body>
        <div class="wrapper">
            <div class="card">
                <div class="header">
                    <img src="cid:logo_procasa" alt="Procasa">
                </div>
                <div class="content">
                    <p>Hola {nombre},</p>
                    <p>Desde Procasa hemos analizado {texto_prop} (<strong>{codigos_html}</strong>). A pesar de su calidad y buen estándar, la propiedad no ha generado el nivel de movimiento esperado en el último trimestre. El mercado inmobiliario chileno actual exige ajustes para impulsar mayor visibilidad y acelerar el proceso de venta.</p>
                    <div class="highlight-box">
                        <strong>Sobreoferta en el Gran Santiago:</strong> Stock supera las <strong>50.000 unidades en RM</strong> y ~100.000 nacional (CChC, Q3 2025), con tiempo de absorción de ~30 meses —el doble del ideal (14-20 meses)—.<br><br>
                        <strong>Tiempos de Venta Extendidos:</strong> Promedio >180 días nacional (Colliers y Portalinmobiliario, Q3 2025); en comunas como Ñuñoa o Santiago, propiedades >180 días reciben 70% menos visitas.<br><br>
                        <strong>Restricciones Crediticias:</strong> Tasas hipotecarias en 4.2% (Banco Central, octubre 2025), reduciendo ~20% la capacidad de endeudamiento de compradores.<br><br>
                        <strong>Ajustes para Ventas:</strong> Hasta 90% de cierres en RM involucran correcciones de 5-10% (Colliers, 2025), bajando precios para competir en portales.
                    </div>
                    <p>Con tasas proyectadas a ~4% para fines de 2025 (Colliers), hay oportunidades, pero mantener precios actuales podría extender la detención 6-12 meses más. Recomendamos un <strong>ajuste del 7%</strong>, basado en >500 transacciones similares: +300% en consultas iniciales y cierre 40-50% más rápido (datos CChC/Colliers).</p>
                    <table><thead><tr><th>Código</th><th>Tipo</th><th>Estado</th></tr></thead><tbody>{filas}</tbody></table>
                    <p style="text-align:center; font-size:17px; font-weight:700; color:#0F172A; margin:30px 0 20px;">
                        Por favor, seleccione una de las siguientes opciones:
                    </p>
                    <div class="btn-container">
                        <a href="{link_ajuste}" class="btn btn-ajuste">Ajustar precio 7% (Recomendado)</a>
                        <a href="{link_llamada}" class="btn btn-otros">Quiero que me llamen para conversarlo</a>
                        <a href="{link_mantener}" class="btn btn-otros">Mantener precio actual</a>
                        <a href="{link_baja}" class="btn btn-otros">Propiedad ya vendida / No disponible</a>
                    </div>
                    <p style="text-align:center; font-size:14.5px; color:#475569; margin:16px 0 0;">
                        Respuesta inmediata con un clic • Datos basados en fuentes oficiales al noviembre 2025
                    </p>
                </div>
                <div class="footer">
                    <strong>Procasa Jorge Pablo Caro Propiedades</strong><br>
                    Gestión de Cartera Exclusiva<br><br>
                    <span class="telefono">+56 9 4090 4971</span>
                    <div style="margin:12px 0 0;">
                        <a href="https://wa.me/56940904971?text=Hola%20Pablo%2C%20te%20escribo%20por%20la%20propiedad%20{codigos_str.replace(',', '%20')}"
                           class="wa-text" target="_blank">Escríbenos por WhatsApp</a>
                        &nbsp;&nbsp;
                        <a href="https://wa.me/56940904971?text=Hola%20Pablo%2C%20te%20escribo%20por%20la%20propiedad%20{codigos_str.replace(',', '%20')}"
                           target="_blank">
                            <img src="cid:whatsapp_icon" alt="WhatsApp" style="width:36px; height:36px; vertical-align:middle;">
                        </a>
                    </div>
                    <div style="margin-top:20px; font-size:11px; color:#64748B; line-height:1.6;">
                        © 2025 Procasa – Todos los derechos reservados<br>
                        <a href="{link_unsubscribe}">Darse de baja de estos correos</a>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return html

# ==============================================================================
# ENVÍO
# ==============================================================================
def enviar_correo(destinatario, asunto, html_body):
    msg = MIMEMultipart('related')
    msg['From'] = f"Gestión Procasa <{Config.GMAIL_USER}>"
    msg['To'] = destinatario
    msg['Subject'] = asunto
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    attach_images(msg)
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True, "Enviado"
    except Exception as e:
        return False, str(e)

# ==============================================================================
# MAIN (con lookup a universo_obelix para obtener la región)
# ==============================================================================
def main():
    print("="*70)
    print("CAMPAÑA ESTRATEGIA DE PRECIO")
    print(f"MODO PRUEBA: {'SÍ' if MODO_PRUEBA else 'NO'}")
    print("="*70)

    # PIPELINE: unimos contactos con universo_obelix por código para obtener la región
    pipeline = [
        {
            "$match": {
                "tipo": "propietario",
                "email_propietario": {"$exists": True, "$ne": "", "$ne": None},
                "estado": {"$ne": "baja_general"},
                "$or": [
                    {"update_price.campana_nombre": {"$ne": NOMBRE_CAMPANA}},
                    {"update_price.campana_nombre": {"$exists": False}}
                ]
            }
        },
        {
            "$lookup": {
                "from": "universo_obelix",
                "localField": "codigo",
                "foreignField": "codigo",
                "as": "propiedad_info"
            }
        },
        {
            "$unwind": {
                "path": "$propiedad_info",
                "preserveNullAndEmptyArrays": True  # si no existe en obelix, sigue (raro pero por seguridad)
            }
        },
        {
            "$match": {
                "$or": [
                    {"propiedad_info.region": "XIII Región Metropolitana"},
                    {"propiedad_info.region": "Región Metropolitana de Santiago"}  # por si está escrito distinto
                ]
            }
        },
        {
            "$project": {
                "email_propietario": 1,
                "nombre_propietario": 1,
                "codigo": 1,
                "tipo": 1,
                "update_price": 1,
                "_id": 1
            }
        }
    ]

    candidatos = collection.aggregate(pipeline)
    grouped_data = {}

    for doc in candidatos:
        email = doc.get("email_propietario", "").strip().lower()
        codigo = doc.get("codigo", "S/C")
        if codigo in CODIGOS_EXCLUIDOS or "@" not in email:
            continue
        if email not in grouped_data:
            nombre_raw = doc.get("nombre_propietario", "")
            primer_nombre = nombre_raw.strip().split()[0].title() if nombre_raw.strip() else "Cliente"
            grouped_data[email] = {"nombre": primer_nombre, "propiedades": [], "ids": []}
        grouped_data[email]["propiedades"].append({"codigo": codigo, "tipo": doc.get("tipo", "Propiedad")})
        grouped_data[email]["ids"].append(doc["_id"])

    print(f"Correos únicos a enviar: {len(grouped_data)}")

    if not MODO_PRUEBA:
        confirm = input("Escriba 'ENVIAR' para confirmar envío real: ")
        if confirm != "ENVIAR":
            print("Envío cancelado.")
            return

    enviados = 0
    for email_real, data in grouped_data.items():
        if MODO_PRUEBA and enviados >= 1:
            break

        destinatario = EMAIL_PRUEBA_DESTINO if MODO_PRUEBA else email_real
        codigo_principal = data['propiedades'][0]['codigo']
        asunto = f"Revisión precio {codigo_principal} – Últimos días del año"

        html = generar_html(data["nombre"], data["propiedades"], email_real)
        exito, msg = enviar_correo(destinatario, asunto, html)

        ahora = datetime.now(timezone.utc)
        debe_actualizar = not MODO_PRUEBA or email_real.lower() == "p.galleguil@gmail.com"

        if debe_actualizar:
            collection.update_many(
                {"_id": {"$in": data["ids"]}},
                {"$set": {
                    "update_price.campana_nombre": NOMBRE_CAMPANA,
                    "update_price.ultima_actualizacion": ahora,
                    "update_price.canales.email": {"enviado": exito, "fecha": ahora}
                }}
            )
            log.info(f"Actualizado en MongoDB: {email_real}")
        else:
            log.info(f"MODO PRUEBA → No se actualiza MongoDB (no es tu correo)")

        if exito:
            log.info(f"Envío exitoso a {destinatario}")
            enviados += 1
        else:
            log.error(f"Error con {email_real}: {msg}")

        if not MODO_PRUEBA:
            time.sleep(2)

    print(f"Terminado. Enviados: {enviados}")

if __name__ == "__main__":
    main()