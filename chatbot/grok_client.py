# chatbot/grok_client.py
import json
import logging
from openai import OpenAI
from config import Config

logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=Config.DEEPSEEK_API_KEY,
    base_url=Config.DEEPSEEK_BASE_URL,
)


def generar_respuesta(messages: list, tipo: str = "prospecto") -> str:
    try:
        print(f"[DEEPSEEK] Enviando {len(messages)} mensajes al modelo...")
        response = client.chat.completions.create(
            model=Config.DEEPSEEK_MODEL_FAST,
            messages=messages,
            temperature=Config.DEEPSEEK_TEMPERATURE,
            max_tokens=Config.DEEPSEEK_MAX_TOKENS_FAST,
            timeout=Config.DEEPSEEK_TIMEOUT_FAST,
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

    prompt_completo = "\n\n".join(
        f"[{m.get('role', 'unknown').upper()}]\n{m.get('content', '')}"
        for m in structured_messages
    )
    bloque_propiedad = next(
        (
            m.get("content", "")
            for m in structured_messages
            if m.get("role") == "system" and "[DATOS OFICIALES DE LA PROPIEDAD ACTIVA]" in str(m.get("content", ""))
        ),
        ""
    )
    approx_tokens = len(prompt_completo) // 4
    logger.info(
        "[DEEPSEEK PROMPT_META] mensajes=%s tokens_aprox=%s bloque_propiedad_len=%s",
        len(structured_messages),
        approx_tokens,
        len(bloque_propiedad),
    )
    logger.info("[DEEPSEEK PROMPT_HEAD] %s", prompt_completo[:1000])
    logger.info("[DEEPSEEK PROMPT_TAIL] %s", prompt_completo[-1000:])
    if bloque_propiedad:
        logger.info(
            "[DEEPSEEK PROPERTY_PAYLOAD] codigo=%s comuna=%s operacion=%s precio=%s ficha_preview=%s",
            prospecto_actual.get("codigo"),
            prospecto_actual.get("comuna"),
            prospecto_actual.get("operacion"),
            prospecto_actual.get("precio_uf"),
            bloque_propiedad[:500],
        )
    else:
        logger.info("[DEEPSEEK PROPERTY_PAYLOAD] no_property_block_in_prompt")

    try:
        print(f"[DEEPSEEK] Generando respuesta estructurada ({len(structured_messages)} msgs)...")
        logger.info(
            "[DEEPSEEK API PAYLOAD] model=%s max_tokens=%s temperature=%s timeout=%s stream=False response_format=%s",
            Config.DEEPSEEK_MODEL_REASONER, Config.DEEPSEEK_MAX_TOKENS_REASONER, Config.DEEPSEEK_TEMPERATURE, Config.DEEPSEEK_TIMEOUT_REASONER, Config.DEEPSEEK_RESPONSE_FORMAT
        )
        
        kwargs = {}
        if Config.DEEPSEEK_RESPONSE_FORMAT == "json_object":
            kwargs["response_format"] = {"type": "json_object"}

        response = client.chat.completions.create(
            model=Config.DEEPSEEK_MODEL_REASONER,
            messages=structured_messages,
            temperature=Config.DEEPSEEK_TEMPERATURE,
            max_tokens=Config.DEEPSEEK_MAX_TOKENS_REASONER,
            timeout=Config.DEEPSEEK_TIMEOUT_REASONER,
            **kwargs
        )

        try:
            logger.info(f"[DEEPSEEK RAW] {response.model_dump() if hasattr(response, 'model_dump') else response}")
        except Exception as e:
            logger.info(f"[DEEPSEEK RAW] unavailable: {e}")
        try:
            logger.info(f"[DEEPSEEK CHOICES] {response.choices}")
        except Exception as e:
            logger.info(f"[DEEPSEEK CHOICES] unavailable: {e}")

        msg = response.choices[0].message if getattr(response, "choices", None) else None
        try:
            logger.info(f"[DEEPSEEK CONTENT] {getattr(msg, 'content', None)}")
        except Exception as e:
            logger.info(f"[DEEPSEEK CONTENT] unavailable: {e}")
        try:
            logger.info(f"[DEEPSEEK FINISH] {getattr(response.choices[0], 'finish_reason', None) if getattr(response, 'choices', None) else None}")
        except Exception as e:
            logger.info(f"[DEEPSEEK FINISH] unavailable: {e}")
        try:
            logger.info(f"[DEEPSEEK MESSAGE META] tool_calls={getattr(msg, 'tool_calls', None)} refusal={getattr(msg, 'refusal', None)}")
        except Exception as e:
            logger.info(f"[DEEPSEEK MESSAGE META] unavailable: {e}")

        raw_content = response.choices[0].message.content if getattr(response, "choices", None) else None
        
        finish_reason = getattr(response.choices[0], "finish_reason", "unknown") if getattr(response, "choices", None) else "unknown"
        usage = getattr(response, "usage", None)
        completion_details = getattr(usage, "completion_tokens_details", None) if usage else None
        reasoning_tokens_used = getattr(completion_details, "reasoning_tokens", 0) if completion_details else 0
        content_final = raw_content
        
        logger.info(
            "[DEEPSEEK_USAGE] finish=%s prompt=%s completion=%s reasoning=%s content_len=%s",
            finish_reason,
            getattr(usage, "prompt_tokens", 0) if usage else 0,
            getattr(usage, "completion_tokens", 0) if usage else 0,
            reasoning_tokens_used,
            len(content_final or "")
        )

        logger.info(
            "[DEEPSEEK_CONTENT_SIZE] finish=%s raw_len=%s",
            finish_reason,
            len(raw_content or "")
        )

        logger.info("[DEEPSEEK RAW_CONTENT] %r", raw_content)
        contenido_json_str = (raw_content or "").strip()
        logger.info(f"[DEEPSEEK PARSE_INPUT] {contenido_json_str}")

        if prospecto_actual and bloque_propiedad and raw_content:
            contenido_lower = raw_content.lower()
            mentions = {
                "codigo": str(prospecto_actual.get("codigo", "")).lower() in contenido_lower,
                "comuna": str(prospecto_actual.get("comuna", "")).lower() in contenido_lower if prospecto_actual.get("comuna") else False,
                "tipo": str(prospecto_actual.get("tipo", "")).lower() in contenido_lower if prospecto_actual.get("tipo") else False,
                "precio": str(prospecto_actual.get("precio_uf", "")).lower() in contenido_lower if prospecto_actual.get("precio_uf") else False,
            }
            logger.info("[DEEPSEEK PROPERTY_MENTION] %s", mentions)
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
