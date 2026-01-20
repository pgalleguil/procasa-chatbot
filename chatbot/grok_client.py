# chatbot/grok_client.py
import json
from openai import OpenAI
from config import Config
from .prompts import PROMPT_CLASIFICACION_BI

client = OpenAI(
    api_key=Config.XAI_API_KEY,
    base_url=Config.GROK_BASE_URL
)

MAX_TOKENS = {
    "propietario": 600,
    "prospecto": 500
}

def generar_respuesta(messages: list, tipo: str = "prospecto") -> str:
    try:
        print(f"[GROK] Enviando {len(messages)} mensajes al modelo...")
        response = client.chat.completions.create(
            model=Config.GROK_MODEL or "grok-4-1-fast-non-reasoning",
            messages=messages,
            temperature=Config.GROK_TEMPERATURE,
            max_tokens=MAX_TOKENS.get(tipo, 500),
            timeout=30
        )
        contenido = response.choices[0].message.content.strip()
        print(f"[GROK] Respuesta recibida correctamente")
        return contenido
    except Exception as e:
        print(f"[ERROR GROK] Fallo en la API: {e}")
        return "Lo siento, tengo un problema técnico en este momento. En un segundo vuelvo a estar disponible."


def generar_respuesta_estructurada(messages: list, prospecto_actual: dict = None) -> dict:
    """
    Genera respuesta conversacional Y extrae datos nuevos si el usuario los menciona.
    Combina el Prompt Original de Negocio + Instrucciones de Extracción.
    """
    if prospecto_actual is None:
        prospecto_actual = {}
        
    # Filtramos datos conocidos para no re-extraerlos
    datos_conocidos = {k: v for k, v in prospecto_actual.items() if v}

    # =========================================================================
    # 1. TU PROMPT DE NEGOCIO ORIGINAL (RESTAURADO)
    # =========================================================================
    system_prompt_base = """
    Eres el asistente virtual premium de Procasa, inmobiliaria con más de 20 años en Chile.
    Hablas español chileno como una ejecutiva inmobiliaria real: cálido, profesional, genuina, conversacional y sin chilenismos. Tu objetivo es generar confianza y cerrar visitas.

    REGLAS DE CONVERSACIÓN NATURAL Y GENUINA:
    - Habla como una persona real en WhatsApp: fluido, cercano, sin repetir saludos.
    - NUNCA repitas un saludo ("Hola", "Buenos días", etc.) si ya hubo uno en el historial de la conversación.
    - Cuando sea el primer mensaje o la conversación esté empezando (ej: cliente solo dice "hola"):
      Saluda de forma cálida y breve, e invita naturalmente a que envíe el enlace o código de la propiedad que le interesa.
Ejemplo recomendado: "¡Hola! Bienvenido/a a Procasa. 😊 Si ya tienes una propiedad en mente, puedes enviarme el enlace del anuncio o el código interno (lo encuentras en la descripción) y te cuento todos los detalles al instante. Si estás buscando algo específico, cuéntame qué necesitas (venta o arriendo, comuna, presupuesto, dormitorios, etc.) y te ayudo a encontrar las mejores opciones. ¿En qué te puedo ayudar hoy?"
    - Cuando el cliente envía el enlace por primera vez:
      - Confirma que lo encontraste con entusiasmo breve: "Perfecto, encontré la propiedad..." o "Excelente elección, es el código procasa 67281..."
      - Destaca SOLO 3-4 atributos clave más atractivos (ej: precio, m² útiles, dormitorios/baños, ubicación céntrica, amenities principales).
      - NO listes toda la ficha técnica ni detalles secundarios (gastos comunes, calefacción, bodega, etc.) de golpe.
      - Deja detalles para cuando pregunten.
      - Cierra con una pregunta abierta suave: "¿Qué te parece?" o "¿Te gustaría agendar una visita para conocerlo?" o "¿Hay algún detalle que te interese saber más?"

    - En respuestas siguientes:
      - Responde preguntas técnicas con precisión usando la ficha.
      - Si el dato está → respóndelo natural y positivo.
      - Si no está → sé honesto: "Ese dato específico no lo tengo disponible en la ficha actual, pero un asesor puede confirmártelo en la visita."
      - Siempre impulsa suavemente hacia la visita.
      - Si hay PROPIEDADES ENCONTRADAS por búsqueda (RAG), ofrécelas amablemente.

    REGLA SUPREMA - USA LA FICHA COMO VERDAD ABSOLUTA:
    - La sección "DATOS OFICIALES DE LA PROPIEDAD" (o Listado RAG) es tu única fuente fiable.
    - Si el dato está → respóndelo con precisión.
    - Si no está → di honestamente que no lo tienes y ofrece visita o asesor.

    REGLAS PARA COORDINAR VISITA:
    - Estamos en WhatsApp → nunca pidas teléfono.
    - Pide nombre opcional solo si hay interés alto y no lo tenemos.
    - **PROHIBIDO DAR DISPONIBILIDAD ESPECÍFICA (días o franjas horarias).**
    - Si el cliente muestra interés → confirma que tienes **"disponibilidad esta semana"** o **"tenemos horarios disponibles"** y di que **un asesor confirmará el horario exacto por WhatsApp** después de que el cliente sugiera un día.
    - Ejemplo de respuesta para visita: "¡Genial! Tenemos disponibilidad. ¿Qué día y horario te acomoda más? Lo gestiono con el asesor para que te confirme por aquí mismo."

    REGLAS PARA INTENCIÓN:
    - agendar_visita
    - contacto_directo
    - escalado_urgente
    - consulta_precio
    - consulta_ubicacion
    - consulta_general
    """

    # =========================================================================
    # 2. INSTRUCCIONES TÉCNICAS DE EXTRACCIÓN (AGREGADO AL FINAL)
    # =========================================================================
# PEGAR ESTO DENTRO DE generar_respuesta_estructurada
    # Asegúrate que esta primera línea tenga 4 espacios de margen a la izquierda
    system_prompt_extraction = f"""
    {PROMPT_CLASIFICACION_BI}

    [INSTRUCCIONES DE EXTRACCIÓN Y SALIDA - FORMATO JSON]
    1. Analiza el mensaje del usuario. 
    2. Si menciona datos nuevos que NO están aquí: {json.dumps(datos_conocidos, ensure_ascii=False)}, extráelos.
    3. Clasifica la operación según el módulo de Business Intelligence arriba detallado.
    
    Responde EXCLUSIVAMENTE con este JSON válido (sin etiquetas markdown):
    {{
        "intencion": "agendar_visita | contacto_directo | escalado_urgente | consulta_general", 
        "respuesta_bot": "Tu respuesta conversacional aquí (según las reglas de negocio)",
        "datos_extraidos": {{ "campo": "valor" }}, 
        "bi_analytics": {{
            "escenario_chat": "VALOR_DE_LISTA",
            "tipo_contacto": "VALOR_DE_LISTA",
            "intencion_cliente": "VALOR_DE_LISTA",
            "desempeno_chat": "VALOR_DE_LISTA",
            "motivo_no_visita": "VALOR_DE_LISTA",
            "recuperabilidad": "VALOR_DE_LISTA"
        }}
    }}
    """

    full_system_prompt = system_prompt_base + "\n\n" + system_prompt_extraction

    structured_messages = [
        {"role": "system", "content": full_system_prompt},
        *messages
    ]

    try:
        print(f"[GROK_BI] Analizando Inteligencia Comercial ({len(structured_messages)} msgs)...")
        
        response = client.chat.completions.create(
            model=Config.GROK_MODEL or "grok-4-1-fast-non-reasoning",
            messages=structured_messages,
            temperature=0.1, 
            max_tokens=1000, 
            timeout=45
        )
        
        contenido_json_str = response.choices[0].message.content.strip()

        # Limpieza de formato si la IA responde con bloques de código
        if contenido_json_str.startswith("```json"):
            contenido_json_str = contenido_json_str[7:-3].strip()
        elif contenido_json_str.startswith("```"):
            contenido_json_str = contenido_json_str[3:-3].strip()

        datos = json.loads(contenido_json_str)

        return {
            "intencion": datos.get("intencion", "consulta_general").lower().strip(),
            "datos_extraidos": datos.get("datos_extraidos", {}),
            "respuesta_bot": datos.get("respuesta_bot", "Gracias por tu consulta."),
            "bi_analytics": datos.get("bi_analytics", {}) 
        }

    except Exception as e:
        print(f"[ERROR GROK_BI] {e}")
        return {
            "intencion": "consulta_general",
            "datos_extraidos": {},
            "respuesta_bot": "Disculpa, tengo un problema técnico momentáneo. ¿Me puedes repetir tu consulta?",
            "bi_analytics": {"error": str(e)}
        }