# iniciar_chat.py
import requests
import json
import logging
import re
from datetime import datetime, timezone
from pymongo import MongoClient
from config import Config

# Configuración de Logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# CONEXIÓN A BASE DE DATOS
# ==========================================
try:
    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    
    # Colección principal de historial (donde el bot lee)
    collection_conversations = db[Config.COLLECTION_CONVERSATIONS] # "conversaciones_whatsapp"
    
    # Colección de usuarios (para guardar el nombre y que el bot sepa quién es)
    collection_prospectos = db["conversaciones_whatsapp"] 
    
    logger.info("Conexión a MongoDB exitosa.")
except Exception as e:
    logger.error(f"Error conectando a MongoDB: {e}")
    exit()

# ==========================================
# UTILIDADES
# ==========================================
def extraer_codigo_mlc(link):
    """
    Extrae código de MercadoLibre (Ej: MLC-3083882384 -> MLC3083882384)
    """
    if not link:
        return None
    match = re.search(r"(MLC)-?(\d+)", link, re.IGNORECASE)
    if match:
        return f"{match.group(1).upper()}{match.group(2)}"
    return None

# ==========================================
# FUNCIÓN DE ENVÍO (WASENDERAPI.COM OFICIAL)
# ==========================================
def enviar_whatsapp_api(phone, message):
    """
    Envía el mensaje usando la API oficial de WaSenderAPI.com
    Endpoint: /api/send-message
    Payload: { "to": ..., "text": ... }
    """
    # 1. Construir URL correcta
    base_url = Config.WASENDER_BASE_URL.rstrip('/')
    url = f"{base_url}/send-message"
    
    # 2. Headers correctos (Authorization: Bearer ...)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {Config.WASENDER_TOKEN}"
    }
    
    # 3. Payload correcto
    payload = {
        "to": phone.replace("+", ""), # WaSenderAPI suele pedir formato internacional sin + (o con, depende, probamos sin)
        "text": message
    }
    
    try:
        logger.info(f"Enviando a: {url}")
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        
        if response.status_code == 200:
            resp_json = response.json()
            # WaSenderAPI suele devolver success: true o status: success
            if resp_json.get("status") == "success" or resp_json.get("success") is True or resp_json.get("data", {}).get("status") == "queued":
                logger.info("✅ Mensaje enviado exitosamente por API.")
                return True
            else:
                logger.warning(f"⚠️ API respondió 200 pero indicó error: {resp_json}")
                return True # Asumimos enviado para guardar en DB
        else:
            logger.error(f"❌ Error API ({response.status_code}): {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Excepción al enviar mensaje: {e}")
        return False

# ==========================================
# LÓGICA PRINCIPAL
# ==========================================
def iniciar_conversacion_manual(telefono, nombre, mensaje_inicial, link_propiedad=None, origen="MercadoLibre"):
    
    fecha_actual = datetime.now(timezone.utc)
    codigo_mlc = extraer_codigo_mlc(link_propiedad)
    
    # 1. ACTUALIZAR PROSPECTO (Para que el bot sepa el nombre 'Jorge')
    datos_prospecto = {
        "phone": telefono,
        "nombre": nombre,
        "origen": origen,
        "updated_at": fecha_actual,
        "ultimo_link_visto": link_propiedad
    }
    if codigo_mlc:
        datos_prospecto["codigo_mercadolibre"] = codigo_mlc

    collection_prospectos.update_one(
        {"phone": telefono},
        {"$set": datos_prospecto},
        upsert=True
    )
    logger.info(f"Prospecto {nombre} actualizado en colección 'prospectos'.")

    # 2. ENVIAR MENSAJE FÍSICO
    enviado = enviar_whatsapp_api(telefono, mensaje_inicial)

    if enviado:
        # 3. GUARDAR EN CONVERSACIONES_WHATSAPP (El historial que lee el bot)
        nuevo_mensaje = {
            "phone": telefono,
            "role": "assistant",  # IMPORTANTE: role 'assistant' para simular que el bot lo dijo
            "content": mensaje_inicial,
            "timestamp": fecha_actual,
            "metadata": {
                "tipo": "inicio_manual_proactivo", 
                "origen": origen,
                "codigo_referencia": codigo_mlc
            }
        }
        collection_conversations.insert_one(nuevo_mensaje)
        print(f"\n[ÉXITO] Mensaje guardado en 'conversaciones_whatsapp'.")
        print(f"El Bot ahora tiene memoria de haber enviado: '{mensaje_inicial[:50]}...'")
    else:
        print("\n[ERROR] Falló el envío a la API. Revisa si el Token en .env es correcto.")

# ==========================================
# EJECUCIÓN
# ==========================================
if __name__ == "__main__":
    # ---------------------------------------------------------
    # DATOS MANUALES
    # ---------------------------------------------------------
    TELEFONO_CLIENTE = "56983219804" 
    NOMBRE_CLIENTE = "Jorge" 
    
    LINK_PROPIEDAD = "https://departamento.mercadolibre.cl/MLC-3083882384-departamento-av-el-estero3-oriente-_JM"
    
    # Mensaje formateado
    MENSAJE = (
        f"¡Hola, {NOMBRE_CLIENTE}! Recibí de Mercadolibre.cl tu interés por "
        f"Av. El Estero/3 Oriente, Papudo, Valparaíso {LINK_PROPIEDAD} "
        f"Cuéntame, ¿cómo puedo ayudarte?"
    )
    # ---------------------------------------------------------
    
    print("--- INICIANDO CONTACTO PROACTIVO ---")
    iniciar_conversacion_manual(
        telefono=TELEFONO_CLIENTE,
        nombre=NOMBRE_CLIENTE,
        mensaje_inicial=MENSAJE,
        link_propiedad=LINK_PROPIEDAD,
        origen="MercadoLibre"
    )