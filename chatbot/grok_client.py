# chatbot/grok_client.py
import json
import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from openai import OpenAI
from config import Config

logger = logging.getLogger(__name__)

client = OpenAI(
    api_key=Config.DEEPSEEK_API_KEY,
    base_url=Config.DEEPSEEK_BASE_URL,
    max_retries=0,
)

LOCAL_FALLBACK_TEXT = (
    "Gracias por escribirnos. Recibimos tu consulta, pero en este momento "
    "nuestro asistente está presentando una demora. Tu mensaje quedó "
    "registrado para poder continuar con la atención."
)


@dataclass(frozen=True)
class DeepSeekResult:
    ok: bool
    content: str = ""
    failure_type: str | None = None
    finish_reason: str | None = None
    http_status: int | None = None
    reasoning_len: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    usage: object | None = None


class _DeepSeekCircuitBreaker:
    """Small process-local breaker; Mongo is not part of provider protection."""

    def __init__(self, failure_threshold=3, cooldown_seconds=60):
        self.failure_threshold = max(int(failure_threshold), 1)
        self.cooldown_seconds = max(int(cooldown_seconds), 1)
        self._lock = threading.Lock()
        self.state = "CLOSED"
        self.consecutive_failures = 0
        self.opened_at = None
        self._probe_in_flight = False

    def allow(self):
        now = time.monotonic()
        with self._lock:
            if self.state == "OPEN":
                if now - (self.opened_at or now) < self.cooldown_seconds:
                    return False
                if self._probe_in_flight:
                    return False
                self.state = "HALF_OPEN"
                self._probe_in_flight = True
                self._log()
            elif self.state == "HALF_OPEN":
                if self._probe_in_flight:
                    return False
                self._probe_in_flight = True
            return True

    def success(self):
        with self._lock:
            self.state = "CLOSED"
            self.consecutive_failures = 0
            self.opened_at = None
            self._probe_in_flight = False
            self._log()

    def failure(self, failure_type):
        # Parsing and unexpected application errors are not provider outages.
        provider_failures = {
            "timeout", "empty_response", "length_exhausted",
            "connection_error", "http_5xx", "http_429",
        }
        with self._lock:
            if failure_type not in provider_failures:
                self._probe_in_flight = False
                return
            self.consecutive_failures += 1
            if self.state == "HALF_OPEN" or self.consecutive_failures >= self.failure_threshold:
                self.state = "OPEN"
                self.opened_at = time.monotonic()
            self._probe_in_flight = False
            self._log()

    def snapshot(self):
        with self._lock:
            return {
                "state": self.state,
                "consecutive_failures": self.consecutive_failures,
                "opened_at": self.opened_at,
            }

    def reset_for_tests(self):
        with self._lock:
            self.state = "CLOSED"
            self.consecutive_failures = 0
            self.opened_at = None
            self._probe_in_flight = False

    def _log(self):
        logger.info(
            "[DEEPSEEK_CIRCUIT] state=%s consecutive_failures=%s opened_at=%s",
            self.state, self.consecutive_failures, self.opened_at,
        )


_deepseek_circuit = _DeepSeekCircuitBreaker(
    failure_threshold=int(getattr(Config, "DEEPSEEK_CIRCUIT_FAILURE_THRESHOLD", 3)),
    cooldown_seconds=int(getattr(Config, "DEEPSEEK_CIRCUIT_COOLDOWN_SECONDS", 60)),
)


def _runtime_metric(context, name, amount=1):
    metrics = (context or {}).get("_runtime_metrics")
    if isinstance(metrics, dict):
        metrics[name] = int(metrics.get(name, 0) or 0) + amount


def normalize_fallback_reason(reason: str | None) -> str:
    """Map provider/parser failures to the stable observability vocabulary."""
    value = str(reason or "other").strip().casefold()
    if value in {"timeout", "deepseek_timeout", "queue_llm_timeout"}:
        return "deepseek_timeout"
    if value in {"empty_response", "deepseek_empty_response", "length_exhausted"}:
        return "deepseek_empty_response"
    if value in {"circuit_open", "deepseek_circuit_open"}:
        return "circuit_open"
    if value.startswith("http_") or value in {"connection_error", "provider_error"}:
        return "provider_error"
    return "other"


def _runtime_fallback_reason_metric(context, reason: str | None) -> None:
    metrics = (context or {}).get("_runtime_metrics")
    if not isinstance(metrics, dict):
        return
    canonical = normalize_fallback_reason(reason)
    by_reason = metrics.setdefault("fallback_by_reason", {})
    by_reason[canonical] = int(by_reason.get(canonical, 0) or 0) + 1


def _phone_hash(context):
    value = str((context or {}).get("phone") or "").strip()
    return (context or {}).get("phone_hash") or (
        hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else "unknown"
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
    if context and context.get("shadow_mode"):
        sink = context.get("usage_sink")
        if isinstance(sink, dict):
            prompt_tokens = int(_usage_value(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(_usage_value(usage, "completion_tokens", 0) or 0)
            total_tokens = int(_usage_value(usage, "total_tokens", prompt_tokens + completion_tokens) or 0)
            sink.setdefault("calls", []).append({
                "model": model,
                "status": status,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "latency_ms": int((time.monotonic() - started_at) * 1000),
                "error": error,
                "fallback_used": bool(fallback_used),
                "timeout": bool(timeout),
            })
        # Shadow usage is persisted with its snapshot, not in production
        # event_log, so it cannot be confused with a customer-facing call.
        return
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


def _deepseek_failure_type(error: Exception) -> str:
    status_code = getattr(error, "status_code", None)
    if status_code == 429:
        return "http_429"
    if isinstance(status_code, int) and status_code >= 500:
        return "http_5xx"
    if isinstance(status_code, int) and status_code >= 400:
        return "http_4xx"
    if _is_timeout_error(error):
        return "timeout"
    name = type(error).__name__.casefold()
    if "connection" in name or "connect" in str(error).casefold():
        return "connection_error"
    if "responsevalidation" in name or "json" in name or "parse" in name:
        return "parse_error"
    return "unexpected_error"


def _log_deepseek_response(result: DeepSeekResult, context=None):
    trace_id = (context or {}).get("trace_id") or (context or {}).get("request_correlation_id") or "unknown"
    logger.info(
        "[DEEPSEEK_RESPONSE] trace_id=%s model=%s http_status=%s finish_reason=%s "
        "content_len=%s reasoning_len=%s prompt_tokens=%s completion_tokens=%s "
        "total_tokens=%s latency_ms=%s result=%s",
        trace_id,
        (context or {}).get("model") or "unknown",
        result.http_status,
        result.finish_reason,
        len(result.content or ""),
        result.reasoning_len,
        result.prompt_tokens,
        result.completion_tokens,
        result.total_tokens,
        result.latency_ms,
        "success" if result.ok else (result.failure_type or "error"),
    )
    if not result.ok and result.failure_type != "circuit_open":
        _runtime_metric(context, "deepseek_failures")
    if not result.ok and result.failure_type in {"empty_response", "length_exhausted"}:
        _runtime_metric(context, "deepseek_empty_responses")


def _call_deepseek_safe(messages: list, *, model: str, max_tokens: int, timeout: int,
                        telemetry_context: dict | None = None, extra_kwargs: dict | None = None) -> DeepSeekResult:
    """Call DeepSeek once, with no SDK retries and a typed terminal result."""
    context = dict(telemetry_context or {})
    context["model"] = model
    started_at = time.monotonic()
    if not _deepseek_circuit.allow():
        result = DeepSeekResult(ok=False, failure_type="circuit_open", latency_ms=0)
        _log_deepseek_response(result, context)
        _runtime_metric(context, "deepseek_circuit_open")
        return result

    _runtime_metric(context, "deepseek_inflight")
    try:
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": Config.DEEPSEEK_TEMPERATURE,
            "max_tokens": max_tokens,
            "timeout": timeout,
            "stream": False,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if extra_kwargs:
            kwargs.update(extra_kwargs)
        response = client.chat.completions.create(**kwargs)
        choices = getattr(response, "choices", None) or []
        choice = choices[0] if choices else None
        message = getattr(choice, "message", None) if choice else None
        content = getattr(message, "content", None) if message else None
        content = str(content or "").strip()
        reasoning_content = getattr(message, "reasoning_content", None) if message else None
        finish_reason = getattr(choice, "finish_reason", None) if choice else None
        usage = getattr(response, "usage", None)
        prompt_tokens = int(_usage_value(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(_usage_value(usage, "completion_tokens", 0) or 0)
        total_tokens = int(_usage_value(usage, "total_tokens", prompt_tokens + completion_tokens) or 0)
        result = DeepSeekResult(
            ok=bool(content),
            content=content,
            failure_type=None if content else ("length_exhausted" if finish_reason == "length" else "empty_response"),
            finish_reason=finish_reason,
            http_status=200,
            reasoning_len=len(str(reasoning_content or "")),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=int((time.monotonic() - started_at) * 1000),
            usage=usage,
        )
        if result.ok:
            _deepseek_circuit.success()
        else:
            logger.warning(
                "[DEEPSEEK_EMPTY_RESPONSE] trace_id=%s finish_reason=%s reasoning_len=%s completion_tokens=%s",
                context.get("trace_id") or context.get("request_correlation_id") or "unknown",
                result.finish_reason,
                result.reasoning_len,
                result.completion_tokens,
            )
            _deepseek_circuit.failure(result.failure_type)
        _log_deepseek_response(result, context)
        return result
    except Exception as error:
        failure_type = _deepseek_failure_type(error)
        result = DeepSeekResult(
            ok=False,
            failure_type=failure_type,
            http_status=getattr(error, "status_code", None),
            latency_ms=int((time.monotonic() - started_at) * 1000),
        )
        _deepseek_circuit.failure(failure_type)
        _log_deepseek_response(result, context)
        logger.warning("[DEEPSEEK_CALL_FAILURE] trace_id=%s failure_type=%s error=%s",
                       context.get("trace_id") or context.get("request_correlation_id") or "unknown",
                       failure_type, type(error).__name__)
        return result
    finally:
        _runtime_metric(context, "deepseek_inflight", -1)


def _local_fallback(telemetry_context=None, reason="provider_failure"):
    context = telemetry_context or {}
    _runtime_metric(context, "fallback_sent")
    _runtime_fallback_reason_metric(context, reason)
    logger.warning(
        "[CHATBOT_LOCAL_FALLBACK] trace_id=%s reason=%s phone_hash=%s",
        context.get("trace_id") or context.get("request_correlation_id") or "unknown",
        reason,
        _phone_hash(context),
    )
    return {
        "intencion": "consulta_general",
        "datos_extraidos": {},
        "respuesta_bot": LOCAL_FALLBACK_TEXT,
        "fallback_used": True,
        "fallback_reason": reason,
        "response_source": "local_fallback",
    }


def get_deepseek_circuit_snapshot():
    return _deepseek_circuit.snapshot()


def _reset_deepseek_circuit_for_tests():
    _deepseek_circuit.reset_for_tests()


def generar_respuesta(messages: list, tipo: str = "prospecto", telemetry_context: dict | None = None,
                      fallback_used: bool = False) -> str:
    started_at = time.monotonic()
    result = _call_deepseek_safe(
        messages,
        model=Config.DEEPSEEK_MODEL_FAST,
        max_tokens=Config.DEEPSEEK_MAX_TOKENS_FAST,
        timeout=Config.DEEPSEEK_TIMEOUT_FAST,
        telemetry_context=telemetry_context,
    )
    if result.ok:
        _record_llm_telemetry(
            model=Config.DEEPSEEK_MODEL_FAST, started_at=started_at,
            usage=result.usage, context=telemetry_context,
            fallback_used=fallback_used,
        )
        return result.content
    _record_llm_telemetry(
        model=Config.DEEPSEEK_MODEL_FAST, started_at=started_at,
        context=telemetry_context, status="error", error=result.failure_type,
        fallback_used=True, timeout=result.failure_type == "timeout",
    )
    _runtime_metric(telemetry_context, "fallback_sent")
    _runtime_fallback_reason_metric(telemetry_context, result.failure_type or "provider_error")
    logger.warning(
        "[CHATBOT_LOCAL_FALLBACK] trace_id=%s reason=%s phone_hash=%s",
        (telemetry_context or {}).get("trace_id")
        or (telemetry_context or {}).get("request_correlation_id") or "unknown",
        result.failure_type or "provider_failure",
        _phone_hash(telemetry_context),
    )
    return LOCAL_FALLBACK_TEXT


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
    logger.info(
        "[DEEPSEEK API PAYLOAD] model=%s max_tokens=%s temperature=%s timeout=%s stream=False response_format=%s thinking=disabled",
        Config.DEEPSEEK_MODEL_REASONER, Config.DEEPSEEK_MAX_TOKENS_REASONER,
        Config.DEEPSEEK_TEMPERATURE, Config.DEEPSEEK_TIMEOUT_REASONER,
        Config.DEEPSEEK_RESPONSE_FORMAT,
    )
    kwargs = {}
    if Config.DEEPSEEK_RESPONSE_FORMAT == "json_object":
        kwargs["response_format"] = {"type": "json_object"}

    result = _call_deepseek_safe(
        structured_messages,
        model=Config.DEEPSEEK_MODEL_REASONER,
        max_tokens=Config.DEEPSEEK_MAX_TOKENS_REASONER,
        timeout=Config.DEEPSEEK_TIMEOUT_REASONER,
        telemetry_context=telemetry_context,
        extra_kwargs=kwargs,
    )
    if not result.ok:
        _record_llm_telemetry(
            model=Config.DEEPSEEK_MODEL_REASONER, started_at=started_at,
            usage=result.usage, context=telemetry_context, status="error",
            error=result.failure_type, fallback_used=True,
            timeout=result.failure_type == "timeout",
        )
        return _local_fallback(telemetry_context, result.failure_type or "provider_failure")

    raw_content = result.content
    contenido_json_str = raw_content.strip()
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

    try:
        datos = json.loads(contenido_json_str)
    except (TypeError, ValueError, json.JSONDecodeError):
        _record_llm_telemetry(
            model=Config.DEEPSEEK_MODEL_REASONER, started_at=started_at,
            usage=result.usage, context=telemetry_context, status="error",
            error="parse_error", fallback_used=True,
        )
        return _local_fallback(telemetry_context, "parse_error")

    _record_llm_telemetry(
        model=Config.DEEPSEEK_MODEL_REASONER, started_at=started_at,
        usage=result.usage, context=telemetry_context,
    )
    return {
        "intencion": str(datos.get("intencion", "consulta_general")).lower().strip(),
        "datos_extraidos": datos.get("datos_extraidos", {}),
        "respuesta_bot": datos.get("respuesta_bot", "Gracias por tu consulta."),
        "fallback_used": False,
        "response_source": "deepseek",
    }
