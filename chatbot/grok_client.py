# chatbot/grok_client.py
import json
import logging
import time
from openai import OpenAI
from config import Config

logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=Config.DEEPSEEK_API_KEY,
    base_url=Config.DEEPSEEK_BASE_URL,
)


def _usage_value(usage, name, default=0):
    if usage is None:
        return default
    value = getattr(usage, name, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(name)
    return value if value is not None else default


def _cached_tokens(usage):
    details = getattr(usage, "prompt_tokens_details", None) if usage else None
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details") or usage.get("prompt_token_details")
    cached = _usage_value(details, "cached_tokens", None)
    return int(cached or 0)


def _record_llm_telemetry(*, model, started_at, usage=None, context=None,
                          status="success", error=None, fallback_used=False,
                          timeout=False, retries=None):
    """Persist provider usage metadata only; never persist prompts or PII."""
    try:
        from .storage import record_observability_event
        prompt_tokens = int(_usage_value(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(_usage_value(usage, "completion_tokens", 0) or 0)
        total_tokens = int(_usage_value(usage, "total_tokens", prompt_tokens + completion_tokens) or 0)
        cache_hit = _cached_tokens(usage)
        payload = {
            "provider": "deepseek",
            "model": model,
            "request_correlation_id": (context or {}).get("request_correlation_id") or (context or {}).get("trace_id"),
            "lead_id": (context or {}).get("lead_id"),
            "conversation_id": (context or {}).get("conversation_id"),
            "batch_id": (context or {}).get("batch_id"),
            "latency_ms": int((time.monotonic() - started_at) * 1000),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": max(prompt_tokens - cache_hit, 0),
            "job_id": (context or {}).get("job_id"),
            "retries": int(retries) if retries is not None else None,
            "retries_observable": retries is not None,
            "timeout": bool(timeout),
            "error": error,
            "fallback_used": bool(fallback_used),
            "status": status,
        }
        # No prompt, response, phone, email or RUT is included here.
        record_observability_event("LLM_CALL", payload)
    except Exception:
        logger.exception("[LLM_TELEMETRY] no se pudo persistir metadata de uso")


def _is_timeout_error(error: Exception) -> bool:
    return isinstance(error, TimeoutError) or "timeout" in type(error).__name__.casefold() or "timeout" in str(error).casefold()


def generar_respuesta(messages: list, tipo: str = "prospecto", telemetry_context: dict | None = None,
                      fallback_used: bool = False) -> str:
    started_at = time.monotonic()
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
        _record_llm_telemetry(
            model=Config.DEEPSEEK_MODEL_FAST, started_at=started_at,
            usage=getattr(response, "usage", None), context=telemetry_context,
            fallback_used=fallback_used,
        )
        print("[DEEPSEEK] Respuesta recibida correctamente")
        return contenido
    except Exception as e:
        _record_llm_telemetry(
            model=Config.DEEPSEEK_MODEL_FAST, started_at=started_at,
            context=telemetry_context, status="error", error=type(e).__name__,
            fallback_used=fallback_used,
            timeout=_is_timeout_error(e),
        )
        print(f"[ERROR DEEPSEEK] Fallo en la API: {e}")
        return "Lo siento, tengo un problema técnico en este momento. En un segundo vuelvo a estar disponible."


def generar_respuesta_estructurada(messages: list, prospecto_actual: dict = None,
                                    telemetry_context: dict | None = None) -> dict:
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
    - Nunca pidas nombre, RUT o correo antes de una intención operacional clara de visita y una oferta opcional aceptada.
    - Si el sistema indica que la oferta fue aceptada, solicita solo el siguiente campo faltante: nombre completo, RUT o correo.
    - Si el cliente entrega varios campos espontáneamente, extráelos y no los vuelvas a pedir.
    - Si el cliente rechaza entregar datos, continúa la atención y no insistas.
    - PROHIBIDO DAR DISPONIBILIDAD ESPECÍFICA (días o franjas horarias).
    - El bot registra el interés y avisa al ejecutivo; nunca confirma una visita, reserva, horario o disponibilidad concreta.
    """

    from .prompts import VISIT_CONFIRMATION_PROMPT
    from .storage import get_pending_response

    # Detect if we need to inject the visit confirmation prompt
    # (when a property has been described and there's no pending confirmation)
    property_code = prospecto_actual.get("codigo") or ""
    has_pending = get_pending_response(
        prospecto_actual.get("phone") or "",
        "VISIT_CONFIRMATION",
    ) if prospecto_actual.get("phone") else None
    inject_visit_prompt = bool(property_code and not has_pending)

    system_prompt_extraction = f"""
    [INSTRUCCIONES DE EXTRACCIÓN Y SALIDA - FORMATO JSON]
    1. Analiza el mensaje del usuario en el contexto de la conversación.
    2. Si menciona explícitamente datos nuevos que NO están aquí: {json.dumps(datos_conocidos, ensure_ascii=False)}, extráelos.
       No infieras duración, financiamiento, documentos ni datos personales con baja confianza.
       Los únicos campos extraíbles son nombre, rut, email, search_duration_bucket,
       financing_status y rental_docs_readiness. Solo captura financing_status si la operación contextual es Venta/Compra;
       solo captura rental_docs_readiness si la operación contextual es Arriendo. Nunca extraigas teléfono, celular,
       WhatsApp o número de contacto.
    OPERACIÓN CONTEXTUAL: {prospecto_actual.get("operacion") or "no informada"}

    CATEGORÍAS DE INTENCIÓN (elige UNA):
    - agendar_visita: El usuario quiere visitar, ver o conocer la propiedad.
      Incluye respuestas afirmativas a invitaciones de visita como "sí, me encantaría".
    - contacto_directo: El usuario pide hablar con un ejecutivo o asesor humano.
    - escalado_urgente: Reclamo, queja, urgencia, problema grave.
    - consulta_general: Cualquier otra consulta, saludos, preguntas iniciales o técnicas.

    Responde EXCLUSIVAMENTE con este JSON válido (sin etiquetas markdown):
    {{
        "intencion": "agendar_visita | contacto_directo | escalado_urgente | consulta_general",
        "respuesta_bot": "Tu respuesta conversacional aquí (según las reglas de negocio)",
        "datos_extraidos": {{ "campo": "valor" }}
    }}
    {VISIT_CONFIRMATION_PROMPT if inject_visit_prompt else ""}
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
    if bloque_propiedad:
        logger.info(
            "[DEEPSEEK PROPERTY_PAYLOAD] codigo=%s comuna=%s operacion=%s precio=%s ficha_len=%s",
            prospecto_actual.get("codigo"),
            prospecto_actual.get("comuna"),
            prospecto_actual.get("operacion"),
            prospecto_actual.get("precio_uf"),
            len(bloque_propiedad),
        )
    else:
        logger.info("[DEEPSEEK PROPERTY_PAYLOAD] no_property_block_in_prompt")

    started_at = time.monotonic()
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

        msg = response.choices[0].message if getattr(response, "choices", None) else None

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

        contenido_json_str = (raw_content or "").strip()

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

        _record_llm_telemetry(
            model=Config.DEEPSEEK_MODEL_REASONER, started_at=started_at,
            usage=usage, context=telemetry_context,
        )

        return {
            "intencion": datos.get("intencion", "consulta_general").lower().strip(),
            "datos_extraidos": datos.get("datos_extraidos", {}),
            "respuesta_bot": datos.get("respuesta_bot", "Gracias por tu consulta."),
        }
    except Exception as e:
        _record_llm_telemetry(
            model=Config.DEEPSEEK_MODEL_REASONER, started_at=started_at,
            context=telemetry_context, status="error", error=type(e).__name__,
            timeout=_is_timeout_error(e),
        )
        print(f"[ERROR DEEPSEEK] {e}")
        try:
            texto = generar_respuesta(
                messages, tipo="prospecto", telemetry_context=telemetry_context,
                fallback_used=True,
            )
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
