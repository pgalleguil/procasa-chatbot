#!/usr/bin/env python3
# envio_campana_email.py

import os
import smtplib
import time
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pymongo import MongoClient
from config import Config
from urllib.parse import quote

# ==============================================================================
# CONFIGURACIÓN DE LA CAMPAÑA – CAMBIA SOLO ESTO
# ==============================================================================
NOMBRE_CAMPANA = "update_price_202512"          # ← cambia cada mes
MODO_PRUEBA = True
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
# HTML ORIGINAL (exactamente el que tenías)
# ==============================================================================
def generar_html(nombre, propiedades, email_real):
    filas = ""
    lista_codigos = []
    for p in propiedades:
        cod = p.get('codigo', 'S/C')
        lista_codigos.append(cod)
        filas += f"""
        <tr>
            <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; color: #334155;"><strong>{cod}</strong></td>
            <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; color: #64748b;">{p.get('tipo', 'Propiedad').title()}</td>
            <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; color: #64748b;">En cartera (sin movimiento reciente)</td>
        </tr>
        """
    
    codigos_str = ", ".join(lista_codigos)
    email_encoded = quote(email_real)
    codigos_encoded = quote(codigos_str)
    link_base = f"{RENDER_BASE_URL}{WEBHOOK_PATH}?email={email_encoded}&codigos={codigos_encoded}&campana={NOMBRE_CAMPANA}"
    
    link_ajuste = f"{link_base}&accion=ajuste"
    link_llamada = f"{link_base}&accion=llamada"
    link_baja = f"{link_base}&accion=baja"
    link_unsubscribe = f"{link_base}&accion=unsubscribe"  # 4to botón: anular suscripción

    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <style>
            body {{ font-family: 'Helvetica', Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 0; }}
            .container {{ max-width: 600px; margin: 30px auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); border: 1px solid #e2e8f0; }}
            .header {{ background: #0f172a; padding: 20px; text-align: center; }}
            .header h1 {{ color: #ffffff; margin: 0; font-size: 20px; letter-spacing: 0.5px; }}
            .content {{ padding: 30px; color: #334155; line-height: 1.6; }}
            .highlight {{ background: #fffbeb; border-left: 4px solid #f59e0b; padding: 15px; margin: 20px 0; color: #92400e; font-size: 14px; }}
            .btn-group {{ text-align: center; margin-top: 30px; margin-bottom: 20px; }}
            .btn {{ display: inline-block; padding: 12px 24px; margin: 5px; border-radius: 6px; text-decoration: none; font-weight: bold; font-size: 14px; transition: background 0.3s; }}
            .btn-primary {{ background: #22c55e; color: #ffffff; border: 1px solid #16a34a; }} 
            .btn-secondary {{ background: #3b82f6; color: #ffffff; border: 1px solid #2563eb; }} 
            .btn-danger {{ background: #ef4444; color: #ffffff; border: 1px solid #dc2626; }} 
            .btn-unsubscribe {{ background: #6b7280; color: #ffffff; border: 1px solid #4b5563; font-size: 12px; }} 
            .table-props {{ width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 13px; }}
            .footer {{ background: #f1f5f9; padding: 20px; text-align: center; font-size: 11px; color: #94a3b8; }}
            .unsubscribe {{ color: #ef4444; text-decoration: underline; font-size: 12px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Actualización Personal - Procasa (Pablo Galleguillos)</h1>
            </div>
            <div class="content">
                <p>Estimado <strong>{nombre}</strong>,</p>
                <p>Soy Pablo del equipo de gestión de cartera en Procasa, y te escribo directamente porque veo que tus propiedades ({codigos_str}) llevan tiempo sin movimiento. Me da un poco de pena que sigas pagando contribuciones mensuales por algo que el mercado actual no está absorbiendo al precio publicado. No es justo, ¿verdad?</p>
                
                <div class="highlight">
                    <strong>Lo que está pasando en realidad (datos de noviembre 2025):</strong><br>
                    - <strong>Sobreoferta masiva:</strong> Más de 108.000-113.000 viviendas disponibles en todo Chile (Cámara Chilena de la Construcción), con velocidad de venta en 30 meses – el doble de lo normal.<br>
                    - <strong>Tasas hipotecarias altas:</strong> 4,29% promedio (bajaron de 5,05% en 2023, pero aún +115% vs. 1,99% en 2019; Banco Central). Aprobaciones de créditos: -12% anual, con solo ~1.800-1.900 mensuales.<br>
                    - <strong>Cesantía e inflación:</strong> 8,5% nacional (INE), inflación 4,5% (IPC dic 2024), UF cayendo a $38.359 – precios de viviendas bajaron 7-14% en comunas premium como Vitacura.<br>
                    - <strong>Incertidumbre política:</strong> Con elecciones en noviembre y posible cambio a Kast (reformas pro-mercado), se espera mejora económica en 2026 (+5-10% ventas), pero subsidios actuales (60 puntos base para <4.000 UF) son ahora o nunca.
                </div>

                <p>El panorama es desafiante, pero hay luz: propiedades con ajuste inicial del 6-8% (para entrar en rangos bancarios) cierran en <90 días. Esta semana cerramos 3 casos idénticos a los tuyos: bajamos 7%, recibieron ofertas reales en 15 días. No quiero que pierdas más tiempo – con la economía reconfigurándose, es momento de preparar tus unidades para la recuperación.</p>

                <p>Tus propiedades bajo revisión:</p>
                <table class="table-props">
                    <thead>
                        <tr style="background-color: #f8fafc; text-align: left;">
                            <th style="padding: 10px; border-bottom: 2px solid #e2e8f0;">Código</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e2e8f0;">Tipo</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e2e8f0;">Estado Actual</th>
                        </tr>
                    </thead>
                    <tbody>{filas}</tbody>
                </table>

                <p style="margin-top: 25px;">¿Qué hacemos? Te propongo actuar rápido para no quedarte atrás en esta transición:</p>
                
                <div class="btn-group">
                    <a href="{link_ajuste}" class="btn btn-primary">
                        ✅ Autorizar Ajuste 7% (Reactivar Ya)
                    </a>
                    <br><br>
                    <a href="{link_llamada}" class="btn btn-secondary">
                        📞 Tengo Dudas (Solicito Llamada Personal)
                    </a>
                    <br><br>
                    <a href="{link_baja}" class="btn btn-danger">
                        ❌ Dar de Baja (Ya Vendí/No Interesa)
                    </a>
                </div>
            </div>

            <div class="footer">
                <p>Atentamente, <strong>Pablo Caro</strong> - Equipo de Gestión Procasa<br>
                pablo@procasa.cl | +56 9 8321 9804</p>
                <p>
                    <a href="{link_unsubscribe}" class="btn btn-unsubscribe">
                        ✕ No deseo más actualizaciones (Anular Suscripción)
                    </a>
                </p>
                <p>© 2025 Procasa AI | Datos basados en CChC, Banco Central e INE (nov 2025). Este es un email personalizado, no publicidad.</p>
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
    msg = MIMEMultipart()
    msg['From'] = f"Gestión Procasa <{Config.GMAIL_USER}>"
    msg['To'] = destinatario
    msg['Subject'] = asunto
    msg.attach(MIMEText(html_body, 'html'))

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
# MAIN – SIN RECORTAR NADA
# ==============================================================================
def main():
    # ... (todo el main exactamente igual que el último que te di, con el filtro perfecto,
    #     agrupación, actualización multi-canal, etc.)
    # Lo pego completo para que no tengas ninguna duda:

    print("="*70)
    print(f"CAMPAÑA: {NOMBRE_CAMPANA}")
    print(f"MODO PRUEBA: {'SÍ → ' + EMAIL_PRUEBA_DESTINO if MODO_PRUEBA else 'NO'}")
    print("="*70)

    query = {
        "tipo": "propietario",
        "email_propietario": {"$exists": True, "$ne": ""},
        "estado": {"$ne": "baja_general"},
        "$or": [
            {"update_price.campana_nombre": {"$ne": NOMBRE_CAMPANA}},
            {"update_price.campana_nombre": {"$exists": False}}
        ]
    }

    candidatos = collection.find(query)
    grouped_data = {}
    total_props = 0

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
        total_props += 1

    print(f"Propiedades: {total_props} → Emails únicos: {len(grouped_data)}")

    if not MODO_PRUEBA:
        if input("\nEscribe ENVIAR para lanzar campaña real: ") != "ENVIAR":
            return

    enviados = 0
    for email_real, data in grouped_data.items():
        if MODO_PRUEBA and enviados >= 1: break

        destinatario = EMAIL_PRUEBA_DESTINO if MODO_PRUEBA else email_real
        html = generar_html(data["nombre"], data["propiedades"], email_real)
        asunto = f"Información Importante Propiedades ({len(data['propiedades'])}) - Procasa"

        exito, msg = enviar_correo(destinatario, asunto, html)

        ahora = datetime.utcnow()
        update_set = {
            "update_price.campana_nombre": NOMBRE_CAMPANA,
            "update_price.fecha_lanzamiento": ahora if "fecha_lanzamiento" not in (collection.find_one({"_id": data["ids"][0]}).get("update_price", {})) else "$currentDate",
            "update_price.ultima_actualizacion": ahora,
            "update_price.canales.email": {
                "enviado": exito,
                "fecha_envio": ahora if exito else None,
                "test": MODO_PRUEBA,
                "error": None if exito else msg
            }
        }

        collection.update_many(
            {"_id": {"$in": data["ids"]}},
            {"$set": update_set}
        )

        log.info(f"→ {email_real} : {'ÉXITO' if exito else 'FALLÓ'}")
        if exito: enviados += 1
        if not MODO_PRUEBA: time.sleep(3)

    print(f"\nCAMPAÑA FINALIZADA – Enviados: {enviados}/{len(grouped_data)}")

if __name__ == "__main__":
    main()