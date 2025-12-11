# chatbot/grok_client.py (MODIFICADO)
import json # <--- AÑADIDO
from openai import OpenAI
from config import Config

client = OpenAI(
    api_key=Config.XAI_API_KEY,
    base_url=Config.GROK_BASE_URL
)

MAX_TOKENS = {
    "propietario": 600,
    "prospecto": 400
}

def generar_respuesta(messages: list, tipo: str = "prospecto") -> str:
    # [CÓDIGO EXISTENTE DE generar_respuesta PARA FLUJOS SIN ESTRUCTURA]
    try:
        print(f"[GROK] Enviando {len(messages)} mensajes al modelo...")
        response = client.chat.completions.create(
            model=Config.GROK_MODEL or "grok-4-1-fast-non-reasoning",
            messages=messages,
            temperature=Config.GROK_TEMPERATURE,
            max_tokens=MAX_TOKENS.get(tipo, 400),
            timeout=30
        )
        contenido = response.choices[0].message.content.strip()
        print(f"[GROK] Respuesta recibida correctamente")
        return contenido
    except Exception as e:
        print(f"[ERROR GROK] Fallo en la API: {e}")
        return "Lo siento, tengo un problema técnico en este momento. En un segundo vuelvo a estar disponible."


def generar_respuesta_estructurada(messages: list, tipo: str = "prospecto") -> dict:
    """
    NUEVA FUNCIÓN: Usa Grok para extraer datos estructurados y generar la respuesta, forzando JSON.
    """
    system_prompt = f"""
    Eres un asistente inmobiliario IA (Procasa) con personalidad cercana pero profesional.
    Tu objetivo es ayudar al cliente, extraer la información clave y generar la respuesta del bot.
    
    ANALIZA el historial y el último mensaje del usuario para:
    1. CLASIFICAR la intención.
    2. EXTRAER NOMBRE, EMAIL y CÓDIGO de propiedad.
    3. GENERAR la respuesta conversacional del bot (campo 'respuesta_bot').

    REGLAS DE CLASIFICACIÓN (Campo 'intencion', debe ser una sola palabra clave en minúscula):
    - agendar_visita
    - consulta_precio
    - consulta_ubicacion
    - contacto_directo
    - escalado_urgente
    - consulta_general (si no es ninguna de las anteriores)

    REGLAS DE EXTRACCIÓN:
    - SÓLO extrae el dato si aparece CLARAMENTE en la conversación o si es inferible como nombre.
    - Si un dato no existe, usa EXÁCTAMENTE el valor: null. (Ej: "nombre": null)
    
    FORMATO DE SALIDA:
    Debes responder ÚNICAMENTE con un objeto JSON válido, siguiendo esta estructura:
    {{
        "intencion": "palabra_clave_intencion",
        "nombre": "Nombre Apellido",
        "email": "correo@ejemplo.com",
        "codigo_propiedad": "Ej: 123456",
        "respuesta_bot": "La respuesta natural del bot para el cliente, lista para enviar."
    }}
    
    Asegúrate de que la salida sea solo el objeto JSON, sin texto explicativo.
    """
    
    structured_messages = [
        {"role": "system", "content": system_prompt},
        *messages[-10:] # Enviamos solo el prompt del sistema y los últimos 10 mensajes
    ]

    try:
        print(f"[GROK_STRUCT] Enviando {len(structured_messages)} mensajes para extracción y respuesta...")
        
        response = client.chat.completions.create(
            model=Config.GROK_MODEL or "grok-4-1-fast-non-reasoning",
            messages=structured_messages,
            temperature=Config.GROK_TEMPERATURE,
            max_tokens=800, # Aumentamos para la estructura JSON + respuesta
            timeout=45,
            # response_format={"type": "json_object"} # Si tu API lo soporta, descomentar
        )
        
        contenido_json_str = response.choices[0].message.content.strip()
        print(f"[GROK_STRUCT] Respuesta JSON recibida.")
        
        try:
            # Limpieza básica para asegurar que solo quede el objeto JSON
            if contenido_json_str.startswith("```json"):
                contenido_json_str = contenido_json_str.strip("`").strip("json").strip()
                
            contenido = json.loads(contenido_json_str)
            # Normalizamos los valores 'null' o 'none' a None de Python
            for key, value in contenido.items():
                 if isinstance(value, str) and value.lower() in ["null", "none", "n/a", "no aplica"]:
                      contenido[key] = None

            return contenido
        except json.JSONDecodeError as e:
            print(f"[ERROR GROK_STRUCT] Fallo al parsear JSON: {e} - Contenido: {contenido_json_str[:200]}...")
            # Fallback en caso de JSON mal formado
            return {
                "intencion": "consulta_general",
                "nombre": None,
                "email": None,
                "codigo_propiedad": None,
                "respuesta_bot": "Lo siento, mi sistema de análisis de datos falló. Por favor, repíteme tu consulta."
            }

    except Exception as e:
        print(f"[ERROR GROK] Fallo en la API estructurada: {e}")
        return {
            "intencion": "consulta_general",
            "nombre": None,
            "email": None,
            "codigo_propiedad": None,
            "respuesta_bot": "Lo siento, tengo un problema técnico en este momento. Vuelvo a estar disponible en un segundo."
        }