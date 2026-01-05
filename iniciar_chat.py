# iniciar_chat.py
import requests
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
    collection_conversations = db[Config.COLLECTION_CONVERSATIONS] 
    logger.info(f"Conexión exitosa a DB: {Config.DB_NAME} | Colección: {Config.COLLECTION_CONVERSATIONS}")
except Exception as e:
    logger.error(f"Error conectando a MongoDB: {e}")
    exit()

# ==========================================
# UTILIDADES DE LIMPIEZA Y EXTRACCIÓN
# ==========================================

def formatear_telefono(phone):
    """Asegura formato +569XXXXXXXX"""
    if not phone: return ""
    limpio = re.sub(r'\D', '', str(phone))
    if not limpio.startswith('56'):
        limpio = f"56{limpio}"
    return f"+{limpio}"

def extraer_codigo_referencia(link):
    """
    Extrae el código MLC o Referencia de Procasa del link.
    """
    if not link: return None
    # Prioridad MLC
    match_mlc = re.search(r"(MLC)-?(\d+)", link, re.IGNORECASE)
    if match_mlc:
        return f"{match_mlc.group(1).upper()}{match_mlc.group(2)}"
    
    # Si no es MLC, buscar código numérico de Procasa al final del link
    match_procasa = re.search(r"(\d+)(?:-|$)", link)
    if match_procasa:
        return match_procasa.group(1)
    
    return None

def obtener_fecha_iso():
    """Formato ISO con Z"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

# ==========================================
# FUNCIÓN DE ENVÍO (WASENDERAPI.COM)
# ==========================================

def enviar_whatsapp_api(phone, message):
    url = f"{Config.WASENDER_BASE_URL.rstrip('/')}/send-message"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {Config.WASENDER_TOKEN}"
    }
    payload = {"to": phone.replace("+", ""), "text": message}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        return response.status_code == 200
    except:
        return False

# ==========================================
# LÓGICA DE INYECCIÓN (CORREGIDA)
# ==========================================

def iniciar_conversacion_manual(telefono_raw, nombre, link, origen="MercadoLibre"):
    telefono_con_plus = formatear_telefono(telefono_raw)
    fecha_iso = obtener_fecha_iso()
    codigo_ref = extraer_codigo_referencia(link)
    
    mensaje_inicial = (
        f"¡Hola, {nombre}! Recibí de Mercadolibre.cl tu interés por la propiedad con el link: "
        f"{link}. Cuéntame, ¿cómo puedo ayudarte?"
    )

    # 1. Limpieza de duplicados sin el '+'
    db[Config.COLLECTION_CONVERSATIONS].delete_many({"phone": telefono_con_plus.replace("+", "")})

    # 2. Envío de WhatsApp
    if enviar_whatsapp_api(telefono_con_plus, mensaje_inicial):
        
        # 3. Preparar campos para el UPDATE
        # Para evitar el conflicto del error anterior, manejamos 'propiedades_vistas' solo con $addToSet
        
        prospecto_data = {
            "nombre": nombre,
            "origen": origen,
            "metodo_ingreso": "MANUAL_ADMIN",
            "ultimo_link_visto": link,
            "codigo_referencia": codigo_ref,
            "lead_score": 2, 
            "updated_at": fecha_iso
        }

        nuevo_mensaje_obj = {
            "role": "assistant",
            "content": mensaje_inicial,
            "timestamp": fecha_iso,
            "tipo": "inicio_manual_proactivo",
            "metadata": {
                "fuente": "Admin_Manual",
                "codigo_referencia": codigo_ref
            }
        }

        # 4. ACTUALIZACIÓN EN MONGODB (CORREGIDA)
        try:
            collection_conversations.update_one(
                {"phone": telefono_con_plus},
                {
                    "$setOnInsert": {
                        "created_at": fecha_iso,
                        "propiedades_vistas": [] # Iniciamos vacío si es nuevo
                    },
                    "$push": {"messages": nuevo_mensaje_obj},
                    "$set": {
                        "updated_at": fecha_iso,
                        "prospecto": prospecto_data,
                        "phone": telefono_con_plus
                    }
                },
                upsert=True
            )
            
            # Operación separada para el código de propiedad para evitar conflictos de MongoDB
            if codigo_ref:
                collection_conversations.update_one(
                    {"phone": telefono_con_plus},
                    {"$addToSet": {"propiedades_vistas": codigo_ref}}
                )

            print(f"✅ EXITOSO: {nombre} ({telefono_con_plus}) | Ref: {codigo_ref}")
        except Exception as e:
            logger.error(f"Error en MongoDB: {e}")
    else:
        print(f"❌ ERROR: El mensaje no pudo ser enviado a {nombre}")

# ==========================================
# CARGA DE DATOS
# ==========================================

if __name__ == "__main__":
    LISTA_PROSPECTOS = [
        {
            "telefono": "56987161016", 
            "nombre": "Matías Ignacio Vásquez Moya", 
            "link": "https://casa.mercadolibre.cl/MLC-3436787494-casa-padre-hurtadonahuel-_JM"
        }
    ]

    for p in LISTA_PROSPECTOS:
        iniciar_conversacion_manual(p["telefono"], p["nombre"], p["link"])