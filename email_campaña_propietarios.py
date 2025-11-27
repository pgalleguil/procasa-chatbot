#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import smtplib
import time
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pymongo import MongoClient
from dotenv import load_dotenv

# Cargar variables de entorno (.env debe tener GMAIL_USER, GMAIL_PASSWORD, MONGO_URI)
load_dotenv()

# ==============================================================================
# ⚙️ CONFIGURACIÓN DE LA CAMPAÑA
# ==============================================================================

# 🛑 IMPORTANTE:
# True: Envía UN SOLO correo a TU email de prueba y registra la ejecución de PRUEBA en MongoDB.
# False: Envía correos REALES a los clientes y registra la ejecución de PRODUCCIÓN en MongoDB.
MODO_PRUEBA = True 
EMAIL_PRUEBA_DESTINO = "p.galleguil@gmail.com"

# --- IDENTIFICADOR DE CAMPAÑA (CLAVE PARA HISTORIAL 2026/2027) ---
NOMBRE_CAMPANA = "baja_precio_nov_2025"

# --- LISTA NEGRA (Códigos de propiedades a excluir) ---
# Aquí van los códigos que no deben ser contactados
CODIGOS_EXCLUIDOS = ["12345", "PRO-999"]

# Credenciales y Configuración de Envío
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")
EMAIL_REMITENTE = f"Gestión Procasa <{GMAIL_USER}>"
EMAIL_RESPUESTA = "jorge@procasa.cl" # Email donde llegan las respuestas de los botones

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger()

# ==============================================================================
# 🔌 CONEXIÓN BASE DE DATOS
# ==============================================================================
client = MongoClient(os.getenv("MONGO_URI"))
db = client["URLS"] 
collection = db["contactos"]

# ==============================================================================
# 🎨 GENERACIÓN DE CORREO HTML CON BOTONES
# ==============================================================================
def generar_html(nombre, propiedades):
    """
    Genera el HTML con tabla de propiedades y Botones de Acción Rápida.
    """
    
    # Crear filas de la tabla
    filas = ""
    lista_codigos = []
    for p in propiedades:
        cod = p.get('codigo', 'S/C')
        lista_codigos.append(cod)
        filas += f"""
        <tr>
            <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; color: #334155;"><strong>{cod}</strong></td>
            <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; color: #64748b;">{p.get('tipo', 'Propiedad').title()}</td>
            <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; color: #64748b;">En cartera</td>
        </tr>
        """
    
    codigos_str = ", ".join(lista_codigos)

    # Lógica de los Botones (Mailto Links)
    # Estos links pre-definen el asunto y cuerpo del email de respuesta del cliente.
    
    subject_ok = f"✅ AUTORIZO AJUSTE - {nombre} ({codigos_str})"
    body_ok = "Hola, autorizo ajustar el precio sugerido (7%) para acelerar la venta."
    link_ok = f"mailto:{EMAIL_RESPUESTA}?subject={subject_ok}&body={body_ok}"

    subject_call = f"📞 SOLICITO LLAMADA - {nombre} ({codigos_str})"
    body_call = "Hola, tengo dudas. Por favor llámenme para revisar la estrategia."
    link_call = f"mailto:{EMAIL_RESPUESTA}?subject={subject_call}&body={body_call}"

    subject_stop = f"🛑 BAJA DE SUSCRIPCIÓN - {nombre}"
    body_stop = "Por favor no enviar más correos de seguimiento, la propiedad ya no está disponible o no deseo correos."
    link_stop = f"mailto:{EMAIL_RESPUESTA}?subject={subject_stop}&body={body_stop}"

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
            .btn-primary {{ background: #22c55e; color: #ffffff; border: 1px solid #16a34a; }} /* Verde */
            .btn-secondary {{ background: #3b82f6; color: #ffffff; border: 1px solid #2563eb; }} /* Azul */
            .table-props {{ width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 13px; }}
            .footer {{ background: #f1f5f9; padding: 20px; text-align: center; font-size: 11px; color: #94a3b8; }}
            .unsubscribe {{ color: #ef4444; text-decoration: underline; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Estrategia Comercial 2025</h1>
            </div>
            <div class="content">
                <p>Hola <strong>{nombre}</strong>,</p>
                <p>Para asegurar la venta de tus propiedades antes del cierre de año fiscal, nuestro equipo de análisis de datos recomienda una actualización estratégica.</p>
                
                <div class="highlight">
                    <strong>Dato de Mercado:</strong> Las propiedades que ajustaron su valor un 7% este mes aumentaron sus solicitudes de visita en un 300% en los portales inmobiliarios.
                </div>

                <p>Estamos monitoreando las siguientes unidades a tu nombre:</p>

                <table class="table-props">
                    <thead>
                        <tr style="background-color: #f8fafc; text-align: left;">
                            <th style="padding: 10px; border-bottom: 2px solid #e2e8f0;">Código</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e2e8f0;">Tipo</th>
                            <th style="padding: 10px; border-bottom: 2px solid #e2e8f0;">Estado</th>
                        </tr>
                    </thead>
                    <tbody>
                        {filas}
                    </tbody>
                </table>

                <p style="margin-top: 25px;">Por favor, selecciona una opción rápida para actualizar nuestro sistema:</p>

                <div class="btn-group">
                    <a href="{link_ok}" class="btn btn-primary">
                        ✅ Autorizar Ajuste Sugerido
                    </a>
                    <br><br>
                    <a href="{link_call}" class="btn btn-secondary">
                        📞 Prefiero una Llamada
                    </a>
                </div>
            </div>

            <div class="footer">
                <p>Este correo fue enviado automáticamente por el sistema de gestión de Procasa.</p>
                <p>
                    <a href="{link_stop}" class="unsubscribe">
                        Ya vendí o no quiero recibir correos (Dar de baja)
                    </a>
                </p>
                <p>© 2025 Procasa IA</p>
            </div>
        </div>
    </body>
    </html>
    """
    return html

def enviar_correo(destinatario, asunto, html_body):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_REMITENTE
    msg['To'] = destinatario
    msg['Subject'] = asunto
    msg.attach(MIMEText(html_body, 'html'))

    try:
        # Configuración SMTP para Gmail
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True, "Enviado exitosamente"
    except Exception as e:
        return False, str(e)

# ==============================================================================
# 🚀 LÓGICA PRINCIPAL - CORREGIDA
# ==============================================================================
def main():
    if not GMAIL_USER or not GMAIL_PASSWORD:
        log.error("❌ Faltan credenciales GMAIL_USER o GMAIL_PASSWORD en .env")
        return

    print("="*60)
    print(f" CAMPAÑA: {NOMBRE_CAMPANA}")
    print(f" MODO PRUEBA: {'ACTIVADO (Solo se enviará 1 ejemplo a ' + EMAIL_PRUEBA_DESTINO + ')' if MODO_PRUEBA else 'DESACTIVADO (Envíos Reales)'}")
    print("="*60)

    # 1. Buscar Contactos
    query = {
        "tipo": "propietario",
        "email_propietario": {"$exists": True, "$ne": ""},
        # Filtro: No enviar si ya fue marcado como enviado en modo producción
        f"campanas.{NOMBRE_CAMPANA}.enviado": {"$ne": True},
        "estado": {"$ne": "baja_general"} 
    }
    
    # 2. Agrupar por Email
    candidatos = collection.find(query)
    grouped_data = {} # { "email": { "nombre": "Juan", "props": [], "ids_mongo": [] } }

    count_props = 0
    for doc in candidatos:
        email = doc.get("email_propietario", "").strip().lower()
        codigo = doc.get("codigo", "S/C")
        
        if codigo in CODIGOS_EXCLUIDOS or "@" not in email:
            continue

        if email not in grouped_data:
            # === CORRECCIÓN DEL ERROR DE INDEX ===
            nombre_raw = doc.get("nombre_propietario", "")
            nombre_parts = nombre_raw.strip().split()
            
            # Si nombre_parts es [], se asigna "Cliente". Esto evita el IndexError.
            primer_nombre = nombre_parts[0].title() if nombre_parts else "Cliente"
            
            grouped_data[email] = {
                "nombre": primer_nombre,
                "propiedades": [],
                "ids_mongo": []
            }
            # =====================================

        grouped_data[email]["propiedades"].append({
            "codigo": codigo,
            "tipo": doc.get("tipo", "Propiedad") 
        })
        grouped_data[email]["ids_mongo"].append(doc["_id"])
        count_props += 1

    total_emails = len(grouped_data)
    
    if total_emails == 0:
        log.warning("⚠️ No hay contactos pendientes para esta campaña.")
        return

    print(f"\nResumen:")
    print(f"- Propiedades procesadas: {count_props}")
    print(f"- Correos únicos a enviar: {total_emails}")
    
    if not MODO_PRUEBA:
        confirm = input("\n¿Escribe 'ENVIAR' para lanzar la campaña real?: ")
        if confirm.strip().upper() != "ENVIAR":
            print("Cancelado.")
            return

    # 3. Proceso de Envío
    enviados_count = 0
    items_a_enviar = list(grouped_data.items())

    for email_real, data in items_a_enviar:
        
        # Lógica para MODO PRUEBA: Solo se ejecuta el primer envío y se detiene.
        if MODO_PRUEBA and enviados_count >= 1:
            break
            
        nombre = data["nombre"]
        props = data["propiedades"]
        ids = data["ids_mongo"]

        # Determinar el destinatario
        destinatario_final = EMAIL_PRUEBA_DESTINO if MODO_PRUEBA else email_real
        asunto = f"Información Importante Propiedades ({len(props)}) - Procasa"

        log.info(f"Procesando: {email_real} ({len(props)} props) -> Destino: {destinatario_final}")

        # Generar contenido y enviar
        html_body = generar_html(nombre, props)
        exito, msg = enviar_correo(destinatario_final, asunto, html_body)

        # 4. Actualizar Base de Datos
        if exito:
            log.info("   ✅ Correo enviado. Registrando intento en DB...")
            
            # Base de datos de log genérico
            db_update = {
                f"campanas.{NOMBRE_CAMPANA}.fecha_envio": datetime.now(),
                f"campanas.{NOMBRE_CAMPANA}.email_destino": destinatario_final,
                f"campanas.{NOMBRE_CAMPANA}.exitoso": True,
                f"campanas.{NOMBRE_CAMPANA}.mensaje_error": None,
                "ultima_accion": f"email_{NOMBRE_CAMPANA}"
            }
            
            if MODO_PRUEBA:
                # MODO PRUEBA: Usa un campo de prueba para NO bloquear la campaña real, pero registra el formato.
                db_update[f"campanas.{NOMBRE_CAMPANA}.test_ejecutado"] = True
                
            else:
                # MODO PRODUCCIÓN: Activa el flag definitivo de "enviado" para no repetir.
                db_update[f"campanas.{NOMBRE_CAMPANA}.enviado"] = True
                
            # Ejecutar la actualización
            collection.update_many(
                {"_id": {"$in": ids}},
                {"$set": db_update}
            )
            
            log.info("   DB actualizada.")
            enviados_count += 1
            
        else:
            log.error(f"   ❌ Fallo: {msg}")
            
            # Registrar el error en DB
            error_update = {
                f"campanas.{NOMBRE_CAMPANA}.fecha_intento": datetime.now(),
                f"campanas.{NOMBRE_CAMPANA}.exitoso": False,
                f"campanas.{NOMBRE_CAMPANA}.mensaje_error": msg,
            }
            if not MODO_PRUEBA:
                 # Solo marcamos como fallido si no estamos en modo prueba, para no bloquear la campaña real
                error_update[f"campanas.{NOMBRE_CAMPANA}.intento_fallido"] = True
            
            collection.update_many(
                {"_id": {"$in": ids}},
                {"$set": error_update}
            )

        # Pausa solo en producción
        if not MODO_PRUEBA:
            time.sleep(3) 

    if MODO_PRUEBA:
        print(f"\n🛑 MODO PRUEBA FINALIZADO. Se envió un solo correo a {EMAIL_PRUEBA_DESTINO}.")
        print("-> Revisa MongoDB (campo 'test_ejecutado') y tu bandeja de entrada.")
    else:
        print("--- CAMPAÑA FINALIZADA ---")

if __name__ == "__main__":
    main()