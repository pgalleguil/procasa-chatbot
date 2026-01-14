# iniciar_chat.py (VERSIÓN CORREGIDA PARA NÚMEROS INTERNACIONALES Y RATE LIMIT)
import requests
import logging
import re
import time  # IMPORTANTE para el delay
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
    collection_propiedades = db[Config.COLLECTION_NAME] 
    logger.info(f"Conexión exitosa a DB: {Config.DB_NAME}")
except Exception as e:
    logger.error(f"Error conectando a MongoDB: {e}")
    exit()

# ==========================================
# UTILIDADES
# ==========================================

def formatear_telefono(phone):
    if not phone: return ""
    # Eliminar todo lo que no sea número
    limpio = re.sub(r'\D', '', str(phone))
    
    # SI el número tiene 9 dígitos, asumimos que es Chile y le falta el 56
    if len(limpio) == 9:
        return f"+56{limpio}"
    
    # Si ya tiene 11 o más dígitos (como 51947850223 o 569...), 
    # solo le ponemos el + adelante
    return f"+{limpio}"

def extraer_codigo_referencia(link):
    if not link: return None
    link = link.upper().replace("_", "-")
    match_mlc = re.search(r"(MLC)-?(\d+)", link)
    if match_mlc: return f"{match_mlc.group(1)}{match_mlc.group(2)}"
    match_yapo = re.search(r"/(\d{8,12})$", link)
    if match_yapo: return match_yapo.group(1)
    match_procasa = re.search(r"(\d{4,6})", link)
    if match_procasa: return match_procasa.group(1)
    return None

def obtener_fecha_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def enviar_whatsapp_api(phone, message):
    url = f"{Config.WASENDER_BASE_URL.rstrip('/')}/send-message"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {Config.WASENDER_TOKEN}"
    }
    payload = {"to": phone.replace("+", ""), "text": message}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code == 200:
            return True
        logger.error(f"Error API WA: {response.text}")
        return False
    except Exception as e:
        logger.error(f"Excepción envío WA: {e}")
        return False

# ==========================================
# LÓGICA PRINCIPAL
# ==========================================

def iniciar_conversacion_manual(telefono_raw, nombre, link, origen="MercadoLibre"):
    telefono_con_plus = formatear_telefono(telefono_raw)
    fecha_iso = obtener_fecha_iso()
    codigo_ref = extraer_codigo_referencia(link)
    
    # BUSCAR PROPIEDAD EN DB
    datos_propiedad = {}
    propiedad_db = None
    
    if codigo_ref:
        query = {
            "$or": [
                {"codigo": codigo_ref}, 
                {"codigo": int(codigo_ref) if codigo_ref.isdigit() else 0},
                {"codigo_mercadolibre": codigo_ref},
                {"codigo_yapo": codigo_ref}
            ]
        }
        propiedad_db = collection_propiedades.find_one(query)
    
    codigo_procasa = None
    if propiedad_db:
        logger.info(f"✅ Propiedad encontrada en DB: {propiedad_db.get('codigo')}")
        datos_propiedad = {
            "codigo": str(propiedad_db.get("codigo")),
            "operacion": propiedad_db.get("operacion"),
            "tipo": propiedad_db.get("tipo"),
            "comuna": propiedad_db.get("comuna"),
            "precio_uf": propiedad_db.get("precio_uf"),
            "dormitorios": propiedad_db.get("dormitorios")
        }
        codigo_procasa = str(propiedad_db.get("codigo"))
    else:
        logger.warning(f"⚠️ Propiedad {codigo_ref} NO encontrada en DB local.")
        codigo_procasa = codigo_ref

    mensaje_inicial = (
        f"¡Hola, {nombre}! Recibí de {origen} tu interés por la propiedad: "
        f"{link}. Cuéntame, ¿cómo puedo ayudarte?"
    )

    if enviar_whatsapp_api(telefono_con_plus, mensaje_inicial):
        prospecto_data = {
            "nombre": nombre,
            "origen": origen,
            "metodo_ingreso": "MANUAL_ADMIN",
            "ultimo_link_visto": link,
            "codigo_referencia": codigo_ref,
            "lead_score": 2, 
            "updated_at": fecha_iso,
            **datos_propiedad 
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

        try:
            collection_conversations.update_one(
                {"phone": telefono_con_plus},
                {
                    "$setOnInsert": {
                        "created_at": fecha_iso,
                        "propiedades_vistas": [] 
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
            
            if codigo_procasa:
                collection_conversations.update_one(
                    {"phone": telefono_con_plus},
                    {"$addToSet": {"propiedades_vistas": codigo_procasa}}
                )

            print(f"✅ EXITOSO: {nombre} ({telefono_con_plus}) | Propiedad Cargada: {codigo_procasa}")
        except Exception as e:
            logger.error(f"Error en MongoDB: {e}")
    else:
        print(f"❌ ERROR ENVÍO: {nombre}")

if __name__ == "__main__":
    # LISTA DE PRUEBA
    LISTA_PROSPECTOS = [
        {
            "telefono": "+56951441330", 
            "nombre": "Fabiola Ignacia Milan López", 
            "link": "https://casa.mercadolibre.cl/MLC-3265470740-casa-general-bulnessanto-domingo-_JM"
        }
    ]
    
    for p in LISTA_PROSPECTOS:
        print(f"\n--- Procesando a {p['nombre']} ---")
        iniciar_conversacion_manual(p["telefono"], p["nombre"], p["link"])
        
        # ESPERA DE SEGURIDAD (Para no saturar la API y evitar el bloqueo de 5 seg)
        #print("Esperando 6 segundos para el siguiente envío...")
        #time.sleep(6)