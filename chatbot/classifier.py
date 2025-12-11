# chatbot/classifier.py
import logging
import re
from config import Config
from .utils import limpiar_telefono
from .grok_client import client # NECESARIO para usar la IA de Grok
from pymongo import MongoClient

logger = logging.getLogger(__name__)

# ==========================================
# 1. CLASIFICADOR DE ROL (PROPIETARIO)
# ==========================================

def es_propietario(phone: str) -> tuple[bool, str]:
    """
    Retorna (es_propietario: bool, nombre_encontrado: str o None)
    Busca en colección universo_obelix → campo movil_propietario
    """
    # Conexión Mongo dentro de la función para manejo de recursos
    mongo_client = None
    try:
        mongo_client = MongoClient(Config.MONGO_URI)
        db = mongo_client[Config.DB_NAME]
        
        limpio = limpiar_telefono(phone)
        if not limpio:
            return False, None

        variantes = [
            limpio,
            "56" + limpio,
            "+56" + limpio,
            "0" + limpio,
        ]
        if len(limpio) == 9:
            variantes.append(limpio[1:])  # por si guardaron sin el 9 inicial

        resultado = db[Config.COLLECTION_NAME].find_one(
            {"movil_propietario": {"$in": variantes}},
            {"nombre_propietario": 1, "apellido_paterno_propietario": 1}
        )

        if resultado:
            nombre = f"{resultado.get('nombre_propietario', '')} {resultado.get('apellido_paterno_propietario', '')}".strip()
            nombre = nombre or "Propietario"
            logger.info(f"PROPIETARIO detectado: {phone} → {nombre}")
            return True, nombre

        logger.info(f"PROSPECTO detectado: {phone}")
        return False, None
        
    except Exception as e:
        logger.error(f"Error en es_propietario: {e}")
        return False, None
    finally:
        if mongo_client:
            try: mongo_client.close()
            except: pass

# ==========================================
# 2. CLASIFICADOR DE INTENCIÓN (IA)
# ==========================================

def detectar_intencion_con_ai(mensaje_actual: str, historial_reducido: list) -> str:
    """
    Usa Grok para clasificar la intención del usuario basándose en el contexto.
    Retorna uno de los keys esperados por el sistema.
    """
    try:
        # Preparamos un prompt de clasificación estricto
        system_prompt = """
        Eres un clasificador de intenciones para una inmobiliaria.
        Analiza el último mensaje del usuario en el contexto de la conversación.
        
        CATEGORÍAS VÁLIDAS (Responde SOLO con una de las palabras clave en minúscula):
        - agendar_visita (quiere ver, visitar, conocer, ir a la propiedad)
        - consulta_precio (pregunta valor, uf, gastos comunes, precio)
        - consulta_ubicacion (donde queda, direccion, calle, sector)
        - consulta_financiera (pie, credito, requisitos, subsidio)
        - contacto_directo (quiere que lo llamen, hablar con humano, ejecutivo, asesor)
        - escalado_urgente (reclamo, enojo, estafa, exige gerente, quiere hablar con supervisor, humano, asesor, ejecutivo)
        - consulta_general (saludos, preguntas vagas, o si envía un LINK/CÓDIGO sin decir nada más)

        Regla: Si el usuario envía un LINK o un CÓDIGO numérico solamente, clasifícalo como "consulta_general" para que el bot procese la ficha primero.
        """

        messages = [
            {"role": "system", "content": system_prompt},
        ]
        
        # Agregamos un poco de contexto (últimos 3 mensajes)
        for msg in historial_reducido[-3:]:
            messages.append({"role": msg["role"], "content": str(msg["content"])})
            
        messages.append({"role": "user", "content": mensaje_actual})

        completion = client.chat.completions.create(
            # **********************************************
            # ****** CORRECCIÓN A Config.GROK_MODEL *******
            # **********************************************
            model=Config.GROK_MODEL, 
            messages=messages,
            temperature=0.0, 
            max_tokens=15
        )

        intencion = completion.choices[0].message.content.strip().lower()
        
        # Limpieza y validación de la respuesta de la IA
        validas = [
            "agendar_visita", "consulta_precio", "consulta_ubicacion", 
            "consulta_financiera", "contacto_directo", "escalado_urgente", 
            "consulta_general"
        ]
        
        for v in validas:
            if v in intencion:
                return v
                
        # Fallback si la IA no responde con una palabra clave clara
        return "consulta_general" 

    except Exception as e:
        logger.error(f"Error clasificando la intención con IA (Grok): {e}")
        # Fallback manual de emergencia
        m = mensaje_actual.lower()
        if any(x in m for x in ["visita", "verla", "verlo", "agendar", "ir a ver", "conocer"]):
            return "agendar_visita"
        if any(x in m for x in ["humano", "llamen", "contactar", "asesor"]):
            return "contacto_directo"
        return "consulta_general"