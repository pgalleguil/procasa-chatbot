# chatbot/grok_client.py
import json
from openai import OpenAI
from config import Config

client = OpenAI(
    api_key=Config.DEEPSEEK_API_KEY,
    base_url=Config.DEEPSEEK_BASE_URL,
)


def _model_name() -> str:
    return (Config.DEEPSEEK_MODEL or "deepseek-v4-flash").strip()


MAX_TOKENS = {
    "propietario": 600,
    "prospecto": 500,
}


def generar_respuesta(messages: list, tipo: str = "prospecto") -> str:
    try:
        print(f"[DEEPSEEK] Enviando {len(messages)} mensajes al modelo...")
        response = client.chat.completions.create(
            model=_model_name(),
            messages=messages,
            temperature=Config.DEEPSEEK_TEMPERATURE,
            max_tokens=MAX_TOKENS.get(tipo, 500),
            timeout=30,
        )
        contenido = response.choices[0].message.content.strip()
        print("[DEEPSEEK] Respuesta recibida correctamente")
        return contenido
    except Exception as e:
        print(f"[ERROR DEEPSEEK] Fallo en la API: {e}")
        return "Lo siento, tengo un problema técnico en este momento. En un segundo vuelvo a estar disponible."


def generar_respuesta_estructurada(messages: list, prospecto_actual: dict = None) -> dict:
    """
    Genera respuesta conversacional y extrae datos nuevos si el usuario los menciona.
    Combina el prompt de negocio + instrucciones de extracción.
    """
    if prospecto_actual is None:
        prospecto_actual = {}

    datos_conocidos = {k: v for k, v in prospecto_actual.items() if v}

    system_prompt_base = """
    Eres el asistente virtual premium de Procasa, inmobiliaria con más de 20 años en Chile.
    Hablas español chileno como una ejecutiva inmobiliaria real: cálido, profesional, genuina, conversacional y sin chilenismos. Tu objetivo es generar confianza y cerrar visitas.

    REGLAS DE CONVERSACIÓN NATURAL Y GENUINA:
    - Habla como una persona real en WhatsApp: fluido, cercano, sin repetir saludos.
    - NUNCA repitas un saludo ("Hola", "Buenos días", etc.) si ya hubo uno en el historial de la conversación.
    - Cuando sea el primer mensaje o la conversación esté empezando (ej: cliente solo dice "hola"):
      Saluda de forma cálida y breve, e invita naturalmente a que envíe el enlace o código de la propiedad que le interesa.
    - Cuando el cliente envía el enlace por primera vez:
      - Confirma que lo encontraste con entusiasmo breve.
      - Destaca SOLO 3-4 atributos clave más atractivos.
      - NO listes toda la ficha técnica ni detalles secundarios de golpe.
      - Deja detalles para cuando pregunten.
      - Cierra con una pregunta abierta suave.

    - En respuestas siguientes:
      - Responde preguntas técnicas con precisión usando la ficha.
      - Si el dato está → respóndelo natural y positivo.
      - Si no está → sé honesto.
      - Siempre impulsa suavemente hacia la visita.
      - Si hay PROPIEDADES ENCONTRADAS por búsqueda (RAG), ofrécelas amablemente.

    REGLA SUPREMA - USA LA FICHA COMO VERDAD ABSOLUTA:
    - La sección "DATOS OFICIALES DE LA PROPIEDAD" (o Listado RAG) es tu única fuente fiable.
    - Si el dato está → respóndelo con precisión.
    - Si no está → di honestamente que no lo tienes y ofrece visita o asesor.

    REGLAS PARA COORDINAR VISITA:
    - Estamos en WhatsApp → nunca pidas teléfono.
    - Pide nombre opcional solo si hay interés alto y no lo tenemos.
    - PROHIBIDO DAR DISPONIBILIDAD ESPECÍFICA (días o franjas horarias).
    - Si el cliente muestra interés → confirma que tienes disponibilidad esta semana o horarios disponibles y di que un asesor confirmará el horario exacto por WhatsApp después de que el cliente sugiera un día.
    """

    system_prompt_extraction = f"""
    [INSTRUCCIONES DE EXTRACCIÓN Y SALIDA - FORMATO JSON]
    1. Analiza el mensaje del usuario.
    2. Si menciona datos nuevos que NO están aquí: {json.dumps(datos_conocidos, ensure_ascii=False)}, extráelos.

    Responde EXCLUSIVAMENTE con este JSON válido (sin etiquetas markdown):
    {{
        "intencion": "agendar_visita | contacto_directo | escalado_urgente | consulta_general",
        "respuesta_bot": "Tu respuesta conversacional aquí (según las reglas de negocio)",
        "datos_extraidos": {{ "campo": "valor" }}
    }}
    """

    structured_messages = [
        {"role": "system", "content": system_prompt_base + "\n\n" + system_prompt_extraction},
        *messages,
    ]

    try:
        print(f"[DEEPSEEK] Generando respuesta estructurada ({len(structured_messages)} msgs)...")
        response = client.chat.completions.create(
            model=_model_name(),
            messages=structured_messages,
            temperature=0.1,
            max_tokens=600,
            timeout=45,
        )

        contenido_json_str = response.choices[0].message.content.strip()
        if contenido_json_str.startswith("```json"):
            contenido_json_str = contenido_json_str[7:-3].strip()
        elif contenido_json_str.startswith("```"):
            contenido_json_str = contenido_json_str[3:-3].strip()
        if not contenido_json_str.startswith("{"):
            ini = contenido_json_str.find("{")
            fin = contenido_json_str.rfind("}")
            if ini != -1 and fin != -1 and fin > ini:
                contenido_json_str = contenido_json_str[ini : fin + 1].strip()

        if not contenido_json_str:
            raise ValueError("Respuesta vacia del modelo")
        datos = json.loads(contenido_json_str)

        return {
            "intencion": datos.get("intencion", "consulta_general").lower().strip(),
            "datos_extraidos": datos.get("datos_extraidos", {}),
            "respuesta_bot": datos.get("respuesta_bot", "Gracias por tu consulta."),
        }
    except Exception as e:
        print(f"[ERROR DEEPSEEK] {e}")
        try:
            texto = generar_respuesta(messages, tipo="prospecto")
            return {
                "intencion": "consulta_general",
                "datos_extraidos": {},
                "respuesta_bot": texto or "Disculpa, tuve un problema momentáneo. ¿Me repites tu consulta?",
            }
        except Exception:
            return {
                "intencion": "consulta_general",
                "datos_extraidos": {},
                "respuesta_bot": "Disculpa, tengo un problema técnico momentáneo. ¿Me puedes repetir tu consulta?",
            }
