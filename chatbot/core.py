import logging
import re
import uuid
import json
import asyncio
from datetime import datetime

from config import Config
from .storage import (
    guardar_mensaje, 
    obtener_conversacion, 
    get_db, 
    actualizar_prospecto, 
    obtener_prospecto,
    establecer_nombre_usuario,
    registrar_propiedades_vistas, # NUEVA IMPORTACIÓN (Anti-repetición)
    obtener_propiedades_vistas,    # NUEVA IMPORTACIÓN (Anti-repetición)
    log_event,
    record_observability_event,
    ensure_conversation_id,
    get_visit_data_state,
    update_visit_data_state,
    update_rag_alternative_offer_state,
    update_rag_search_state,
    VISIT_DATA_FIELDS,
)
from .crm_service import CrmService
from .constants import PipelineStage, InteractionType, LeadIntent, CHILE_TZ

from .grok_client import generar_respuesta, generar_respuesta_estructurada
from .link_extractor import analizar_mensaje_para_link, extraer_codigo_internacional, extraer_codigo_toctoc_compuesto, extraer_contexto_urls, URL_RE
from .utils import extraer_rut, extraer_email, safe_int_conversion, extraer_nombre_explicito
from .utils import parse_bool
from .alert_service import send_alert_once
from .classifier import es_propietario, clasificar_corredor_externo
from .processing_service import LeadProcessingService
from .property_lookup import PROPERTY_COLLECTION_NAME, find_property_by_any_identifier, get_prop_location, get_prop_operation

# RAG IMPORT
from .rag import buscar_propiedades, formatear_resultados_texto, buscar_semanticamente, extraer_filtros_estructurados
# Importamos el prompt maestro con las reglas estrictas (No horarios, no inventar)
from .prompts import SYSTEM_PROMPT_PROSPECTO 
from .conversation_policy import (
    should_offer_visit_data,
    classify_visit_data_reply,
    visit_data_fields_missing,
    build_visit_data_prompt,
    visit_data_declined_response,
    alternative_requested,
    alternative_offer_accepted,
    alternative_offer_declined,
    property_rejected,
    extract_spontaneous_lead_signals,
    filter_relaxation_accepted,
    outbound_phone_request,
    safe_phone_free_response,
    outbound_unconfirmed_visit_claim,
    safe_visit_claim_free_response,
    is_substantial_duplicate,
    duplicate_response_fallback,
)

logger = logging.getLogger(__name__)

# ==========================================

# RAG IMPORT
from .rag import buscar_propiedades, formatear_resultados_texto, buscar_semanticamente
# Importamos el prompt maestro con las reglas estrictas (No horarios, no inventar)
from .prompts import SYSTEM_PROMPT_PROSPECTO 

logger = logging.getLogger(__name__)

# ==========================================
#   LEAD SCORE (DELEGADO A CRM SERVICE)
# ==========================================
# La función local calcular_lead_score se ha eliminado en favor de CrmService.calculate_score

# ==========================================
#   FORMATO FICHA TÉCNICA (RESTITUÍDO)
# ==========================================
def format_uf_chilena(value) -> str:
    """Formatea un valor UF (entero o decimal) con notación chilena.

    ``118.03`` → "118,03" ; ``57836`` → "57.836" ; ``11803`` → "11.803".
    """
    if value is None:
        return "N/D"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    if amount == int(amount):
        return f"{int(amount):,}".replace(",", ".")
    s = f"{amount:.2f}".rstrip("0").rstrip(".")
    int_part, _, dec_part = s.partition(".")
    return f"{int(int_part):,}".replace(",", ".") + (f",{dec_part}" if dec_part else "")


def formatear_ficha_tecnica(propiedad: dict, lead_executive: str = None) -> str:
    """
    Toma un diccionario con datos de propiedad y lo formatea como un texto claro
    para inyectarlo al contexto del LLM.
    Se omite al ejecutivo original de la propiedad para evitar falsas expectativas,
    mostrando solo al ejecutivo efectivamente asignado al lead si existe.
    Compatible con el esquema anidado del scraper prop360 (tipo_operacion, ubicacion,
    caracteristicas, observaciones) y con el esquema plano antiguo.
    """
    # ── Sub-documentos del nuevo esquema ─────────────────────────────────────
    tipo_op   = propiedad.get("tipo_operacion") or {}
    ubicacion = propiedad.get("ubicacion") or {}
    caract    = propiedad.get("caracteristicas") or {}
    obs       = propiedad.get("observaciones") or {}
    estado    = propiedad.get("estado") or {}

    # ── Tipo y operación ──────────────────────────────────────────────────────
    tipo      = tipo_op.get("tipo") or propiedad.get("tipo") or "N/D"
    es_venta  = tipo_op.get("venta") or False
    es_arriendo = tipo_op.get("arriendo") or False
    if es_venta:
        operacion = "Venta"
    elif es_arriendo:
        operacion = "Arriendo"
    else:
        operacion = propiedad.get("operacion") or "N/D"

    # ── Precios ───────────────────────────────────────────────────────────────
    precio_bloque_venta   = tipo_op.get("precio_venta") or {}
    precio_bloque_arriendo = tipo_op.get("precio_arriendo") or {}
    precio_bloque = precio_bloque_venta if es_venta else precio_bloque_arriendo

    precio_clp_raw = (
        precio_bloque.get("precio_clp")
        or propiedad.get("precio_clp")
    )
    precio_uf_raw = (
        precio_bloque.get("precio_uf")
        or propiedad.get("precio_uf")
    )
    gastos_comunes_raw = tipo_op.get("gastos_comunes") or propiedad.get("gastos_comunes") or 0

    precio_clp     = safe_int_conversion(precio_clp_raw) or 0
    gastos_comunes = safe_int_conversion(gastos_comunes_raw) or 0
    precio_uf_str  = format_uf_chilena(precio_uf_raw) if precio_uf_raw is not None else "N/D"
    precio_clp_str = f"${precio_clp:,}" if precio_clp else "N/D"

    # ── Ubicación ─────────────────────────────────────────────────────────────
    region  = ubicacion.get("region")  or propiedad.get("region")  or "N/D"
    comuna  = ubicacion.get("comuna")  or propiedad.get("comuna")  or "N/D"
    sector  = ubicacion.get("sector")  or propiedad.get("sector")  or ""

    # ── Características ───────────────────────────────────────────────────────
    dormitorios      = caract.get("dormitorios")                 or propiedad.get("dormitorios")      or "N/D"
    banos            = caract.get("banos")                       or propiedad.get("banos")            or "N/D"
    sup_util         = caract.get("superficie_util")             or propiedad.get("m2_utiles")        or "N/D"
    sup_total        = caract.get("superficie_total")            or propiedad.get("m2_totales")       or "N/D"
    sup_terreno      = caract.get("superficie_terreno")          or propiedad.get("superficie_terreno") or "N/D"
    sup_construida   = caract.get("superficie_construida")       or propiedad.get("superficie_construida") or "N/D"
    sup_terraza      = caract.get("superficie_terraza")          or propiedad.get("m2_terraza")       or "N/D"
    estac_cub        = caract.get("estacionamientos_cubiertos")  or propiedad.get("estacionamientos") or 0
    estac_desc       = caract.get("estacionamientos_descubiertos") or 0
    estacionamientos = (safe_int_conversion(estac_cub) or 0) + (safe_int_conversion(estac_desc) or 0)
    bodegas          = caract.get("bodegas")                     or propiedad.get("bodega")           or 0
    orientacion      = caract.get("orientacion")                 or propiedad.get("orientacion")      or "N/D"
    ano_construccion = caract.get("ano_construccion")            or propiedad.get("ano_construccion") or "N/D"
    num_pisos        = caract.get("numero_pisos")                or propiedad.get("numero_pisos")     or "N/D"

    # ── Observaciones y descripción ───────────────────────────────────────────
    descripcion = (
        obs.get("descripcion")
        or propiedad.get("descripcion_clean")
        or propiedad.get("descripcion")
        or ""
    )
    titulo = obs.get("titulo") or propiedad.get("titulo") or ""

    # ── Ejecutivo (Solo si hay asignación efectiva al lead) ─────────────
    # Se ignora intencionalmente estado.get("ejecutivo") para no confundir al LLM.
    ejecutivo = lead_executive if (lead_executive and lead_executive not in ["No Asignado", "Sin Asignar", None, ""]) else None

    # ── Construir texto ───────────────────────────────────────────────────────
    lineas = [
        f"Código interno:     {propiedad.get('codigo', 'N/D')}",
        f"Tipo:               {tipo}",
        f"Operación:          {operacion}",
        f"Región:             {region}",
        f"Comuna:             {comuna}",
    ]
    if sector:
        lineas.append(f"Sector:             {sector}")
    lineas += [
        f"Precio:             {precio_uf_str} UF | {precio_clp_str}",
    ]
    if gastos_comunes:
        lineas.append(f"Gastos comunes:     ${gastos_comunes:,}")
    lineas += [
        f"Dormitorios:        {dormitorios}",
        f"Baños:              {banos}",
        f"Sup. útil:          {sup_util} m²",
        f"Sup. total:         {sup_total} m²",
    ]
    if sup_terreno and sup_terreno != "N/D":
        lineas.append(f"Sup. terreno:       {sup_terreno} m²")
    if sup_construida and sup_construida != "N/D":
        lineas.append(f"Sup. construida:    {sup_construida} m²")
    if sup_terraza and sup_terraza != "N/D":
        lineas.append(f"Terraza:            {sup_terraza} m²")
    lineas += [
        f"Estacionamientos:   {estacionamientos if estacionamientos else 'N/D'}",
        f"Bodegas:            {bodegas if bodegas else 'No'}",
        f"Orientación:        {orientacion}",
    ]
    if ano_construccion != "N/D":
        lineas.append(f"Año construcción:   {ano_construccion}")
    if num_pisos != "N/D":
        lineas.append(f"N° pisos:           {num_pisos}")
    if ejecutivo:
        lineas.append(f"Ejecutivo asignado: {ejecutivo}")
    if titulo:
        lineas.append(f"Título:             {titulo}")
    if descripcion:
        lineas.append(f"Descripción:        {descripcion[:400]}")

    return "\n    ".join(lineas)


def _buscar_propiedad_en_universo(db, raw_value, portal: str | None = None):
    """
    Intenta resolver una propiedad en universo_cartera usando un valor crudo
    que puede ser código interno, código internacional o URL.
    """
    if not raw_value:
        return None

    value = str(raw_value).strip()
    if not value:
        return None

    value_int = safe_int_conversion(value)
    portal = (portal or "").strip()
    portal_specific_queries = []
    if portal == "Yapo":
        portal_specific_queries.extend([
            {"yapo.url_yapo": value},
            {"yapo.url_yapo": {"$regex": re.escape(value), "$options": "i"}},
            {"yapo.codigo_yapo": value},
            {"yapo.codigo_yapo": value_int},
            {"publicaciones.yapo.url_yapo": value},
            {"publicaciones.yapo.url_yapo": {"$regex": re.escape(value), "$options": "i"}},
            {"url_yapo": value},
            {"url_yapo": {"$regex": re.escape(value), "$options": "i"}},
            {"codigo_yapo": value},
            {"codigo_yapo": value_int},
            {"publicaciones.yapo.codigo_yapo": value},
            {"publicaciones.yapo.codigo_yapo": value_int},
        ])
    elif portal == "MercadoLibre":
        portal_specific_queries.extend([
            {"publicaciones.portal_inmobiliario.url_mercado_libre": value},
            {"publicaciones.portal_inmobiliario.url_mercado_libre": {"$regex": re.escape(value), "$options": "i"}},
            {"codigo_mercadolibre": value},
            {"codigo_mercadolibre": value_int},
            {"publicaciones.portal_inmobiliario.codigo_pi": value},
            {"publicaciones.portal_inmobiliario.codigo_pi": value_int},
            {"codigo_pi": value},
            {"codigo_pi": value_int},
        ])
    elif portal == "PortalInmobiliario":
        portal_specific_queries.extend([
            {"publicaciones.portal_inmobiliario.url_pi": value},
            {"publicaciones.portal_inmobiliario.url_pi": {"$regex": re.escape(value), "$options": "i"}},
            {"publicaciones.portal_inmobiliario.codigo_pi": value},
            {"publicaciones.portal_inmobiliario.codigo_pi": value_int},
            {"codigo_pi": value},
            {"codigo_pi": value_int},
            {"codigo_mercadolibre": value},
            {"codigo_mercadolibre": value_int},
        ])
    elif portal == "TocToc":
        portal_specific_queries.extend([
            {"publicaciones.toctoc.url_toctoc": value},
            {"publicaciones.toctoc.url_toctoc": {"$regex": re.escape(value), "$options": "i"}},
            {"toctoc.enlace": value},
            {"toctoc.enlace": {"$regex": re.escape(value), "$options": "i"}},
        ])
    elif portal == "Procasa":
        portal_specific_queries.extend([
            {"publicaciones.procasa.url_procasa": value},
            {"publicaciones.procasa.url_procasa": {"$regex": re.escape(value), "$options": "i"}},
        ])

    for query in portal_specific_queries:
        prop = db[PROPERTY_COLLECTION_NAME].find_one(query)
        if prop:
            return prop

    return find_property_by_any_identifier(db, value, PROPERTY_COLLECTION_NAME)

# ==========================================
#   PROCESADOR PRINCIPAL
# ==========================================


async def process_user_message(phone: str, message: str, is_from_me: bool = False,
                               telemetry_context: dict | None = None) -> str:
    async def _run_sync(fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    trace_id = str(uuid.uuid4())[:8]
    original_message = message
    msg_lower = original_message.lower()
    logger.info(f"[LINK_TRACE] trace={trace_id} phone={phone} inicio_proceso_mensaje")
    
    # 1. Guardar mensaje (con el rol correcto)
    # Si viene de 'me' (del dueño del bot), lo guardamos como assistant/human
    role = "assistant" if is_from_me else "user"
    await _run_sync(guardar_mensaje, phone, role, original_message)

    # === LÓGICA DE PAUSA (INTERCEPCIÓN) ===
    from .storage import obtener_bot_pausado, toggle_bot_pausado
    
    if original_message.strip() == "..":
        nuevo_estado = toggle_bot_pausado(phone)
        if nuevo_estado:
            logger.info(f"🤖 [TOGGLE] Bot PAUSADO para {phone}")
        else:
            logger.info(f"🤖 [TOGGLE] Bot REACTIVADO para {phone}")
        
        # Guardamos en DB para historial interno pero retornamos vacío para NO enviar a WhatsApp
        await _run_sync(guardar_mensaje, phone, "assistant", f"Bot {'Pausado' if nuevo_estado else 'Reactivado'} (Comando ..)", {"tipo": "bot_toggle"})
        return "" 

    # Si es un mensaje manual del agente (is_from_me), no hacemos nada más, 
    # solo lo dejamos guardado en el historial arriba.
    if is_from_me:
        logger.info(f"[MANUAL] Mensaje manual detectado para {phone}. Guardado en contexto.")
        return ""

    # Si el bot está pausado, NO procesamos ni respondemos para el cliente
    if await _run_sync(obtener_bot_pausado, phone):
        logger.info(f"[PAUSED] Bot pausado para {phone}. Ignorando procesamiento.")
        return "" 

    historial = await _run_sync(obtener_conversacion, phone)

    # === OBTENEMOS PROSPECTO TEMPRANO PARA PODER USARLO EN ORIGEN Y EN TODO EL FLUJO ===
    # NOTA: stage es un campo de nivel superior, no está dentro de 'prospecto'
    db = await _run_sync(get_db)
    lead_doc_full = await _run_sync(lambda: db["leads"].find_one({"phone": phone}) or {})
    if lead_doc_full.get("conversation_status") == "BLOCKED_EXTERNAL_BROKER":
        logger.info("[CORREDOR] Conversación bloqueada; inbound persistido sin respuesta automática.")
        return ""
    if any(phrase in msg_lower for phrase in ("no me escriban", "dejen de escribir", "no quiero mensajes", "stop")):
        await _run_sync(db["leads"].update_one, {"phone": phone}, {"$set": {
            "conversation_status": "STOPPED_BY_CLIENT",
            "conversation_status_reason": "explicit_stop_request",
            "conversation_status_evidence": original_message[:300],
            "conversation_status_at": datetime.now(CHILE_TZ).isoformat(),
            "conversation_status_version": "conversation_stop_v1",
        }})
        return ""
    prospecto_actual = lead_doc_full.get("prospecto", {})
    conversation_id = lead_doc_full.get("conversation_id") or prospecto_actual.get("conversation_id")
    if not conversation_id:
        conversation_id = await _run_sync(ensure_conversation_id, phone)

    async def _persist_generated_outbound(response: str, metadata: dict, *, intent: str | None = None,
                                          lead_doc: dict | None = None) -> str:
        """Persist every queue-bound response in the durable delivery state machine."""
        generation_id = (telemetry_context or {}).get("generation_id") or str(uuid.uuid4())
        batch_id = (telemetry_context or {}).get("batch_id")
        effective_metadata = {
            **(metadata or {}),
            "delivery_status": "generated",
            "batch_id": batch_id,
            "generation_id": generation_id,
        }
        lead = lead_doc or lead_doc_full
        lead_id = str(lead.get("_id")) if lead.get("_id") else None
        try:
            await _run_sync(log_event, phone, InteractionType.BOT_MSG, "bot", {
                "text": response,
                "intencion": intent,
                "delivery_status": "generated",
                "batch_id": batch_id,
                "generation_id": generation_id,
            })
        except Exception as ex_log:
            logger.error(f"Error logging bot event: {ex_log}")
        await _run_sync(guardar_mensaje, phone, "assistant", response, effective_metadata)
        await _run_sync(record_observability_event, "RESPONSE_GENERATED", {
            "conversation_id": conversation_id,
            "lead_id": lead_id,
            "batch_id": batch_id,
            "generation_id": generation_id,
            "intent": intent,
            "response_len": len(response or ""),
        })
        return response

    async def _await_durable_handoff(*, alert_type: str, lead_score: int, criteria: dict,
                                     last_response: str, last_user_msg: str,
                                     full_history: list, window_minutes: int,
                                     lead_type_label: str, alert_phone: str | None = None) -> dict:
        """Wait for the canonical CRM handoff and record its durable outcome."""
        event_base = {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
            "alert_type": alert_type,
        }
        await _run_sync(record_observability_event, "human_handoff_triggered", {
            **event_base,
            "property_id": criteria.get("codigo"),
            "operation": criteria.get("operacion"),
        })
        try:
            result = await send_alert_once(
                phone=alert_phone or phone,
                lead_type=alert_type,
                lead_score=lead_score,
                criteria=criteria,
                last_response=last_response,
                last_user_msg=last_user_msg,
                full_history=full_history,
                window_minutes=window_minutes,
                lead_type_label=lead_type_label,
            )
        except Exception as exc:
            await _run_sync(record_observability_event, "human_handoff_failed", {
                **event_base, "reason": type(exc).__name__,
            })
            logger.error("[HANDOFF] durable handoff failed type=%s", alert_type, exc_info=True)
            return {"status": "failed", "reason": type(exc).__name__}

        result = result if isinstance(result, dict) else {"status": "enqueued"}
        if result.get("status") in {"enqueued", "deduplicated"}:
            await _run_sync(record_observability_event, "human_handoff_enqueued", {
                **event_base,
                "status": result.get("status"),
                "durable": result.get("durable"),
                "assignment_cycle_id": result.get("assignment_cycle_id"),
                "delivery_id": result.get("delivery_id"),
            })
        else:
            await _run_sync(record_observability_event, "human_handoff_failed", {
                **event_base, "reason": result.get("reason") or result.get("status"),
            })
        return result
    visit_data_state = await _run_sync(get_visit_data_state, phone)
    pending_visit_data_reply = classify_visit_data_reply(
        original_message,
        offer_pending=visit_data_state.get("status") in {"offered", "accepted"},
    )
    if pending_visit_data_reply == "accepted":
        visit_data_state = await _run_sync(update_visit_data_state, phone, {
            "status": "accepted",
            "accepted_at": datetime.now(CHILE_TZ).isoformat(),
        })
    elif pending_visit_data_reply == "declined":
        visit_data_state = await _run_sync(update_visit_data_state, phone, {
            "status": "declined",
            "declined_at": datetime.now(CHILE_TZ).isoformat(),
            "declined_property_id": visit_data_state.get("property_id") or prospecto_actual.get("codigo"),
        })

    rag_alternative_state = dict(prospecto_actual.get("rag_alternative_offer") or {})
    alternative_reply = "none"
    if rag_alternative_state.get("status") == "offered":
        if alternative_offer_accepted(original_message, offer_pending=True):
            alternative_reply = "accepted"
            rag_alternative_state = await _run_sync(update_rag_alternative_offer_state, phone, {
                "status": "accepted", "accepted_at": datetime.now(CHILE_TZ).isoformat(),
            })
        elif alternative_offer_declined(original_message, offer_pending=True):
            alternative_reply = "declined"
            rag_alternative_state = await _run_sync(update_rag_alternative_offer_state, phone, {
                "status": "declined", "declined_at": datetime.now(CHILE_TZ).isoformat(),
            })

    async def _mark_visit_data_captured(field_names):
        nonlocal visit_data_state
        fresh = [
            field for field in field_names
            if field in VISIT_DATA_FIELDS
            and field not in set(visit_data_state.get("captured_fields") or [])
        ]
        if not fresh:
            return
        captured = sorted(set(visit_data_state.get("captured_fields") or []).union(fresh))
        visit_data_state = await _run_sync(update_visit_data_state, phone, {
            "captured_fields": captured,
        })
        for field in fresh:
            await _run_sync(record_observability_event, f"visit_data_{field}_captured", {
                "conversation_id": conversation_id,
                "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
            })
        if set(captured) == set(VISIT_DATA_FIELDS) and visit_data_state.get("status") == "accepted":
            visit_data_state = await _run_sync(update_visit_data_state, phone, {
                "status": "completed",
                "completed_at": datetime.now(CHILE_TZ).isoformat(),
            })
            await _run_sync(record_observability_event, "visit_data_completed", {
                "conversation_id": conversation_id,
                "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
            })
    await _run_sync(record_observability_event, "MESSAGE_RECEIVED", {
        "conversation_id": conversation_id,
        "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
        "phone": phone,
        "message": original_message,
        "is_from_me": bool(is_from_me)
    })

    # NUEVO: Lógica de Reactivación (Si estaba archivado y vuelve a escribir)
    # Se considera lead nuevo para efectos de procesamiento.
    if lead_doc_full.get("stage") == "ARCHIVED":
        logger.info(f"🔄 [REACTIVATION] Lead {phone} estaba ARCHIVADO y volvió a escribir. Reactivando...")
        now_str = datetime.now(CHILE_TZ).isoformat()
        
        # Actualizamos campos de nivel superior para que el motor lo vea como nuevo
        await _run_sync(
            db["leads"].update_one,
            {"phone": phone},
            {"$set": {
                "stage": PipelineStage.NEW,
                "pipeline_stage": PipelineStage.NEW,
                "archive_reason": None,
                "reactivated_at": now_str,
                "created_at": now_str, # Reset timestamps to avoid immediate re-archival
                "timestamp": now_str,
                "last_processed_at": None
            }}
        )
        lead_doc_full["stage"] = PipelineStage.NEW # Actualizar copia local

    # Solo forzar WhatsApp como fallback si no hay origen previo (permite Yapo, MercadoLibre, etc.)
    if not prospecto_actual.get("origen"):
        await _run_sync(actualizar_prospecto, phone, {"origen": "WhatsApp"})

    # =======================================================
    # 2. FLUJO PROPIETARIO
    # =======================================================
    es_prop, nombre_prop = await _run_sync(es_propietario, phone)
    if es_prop:
        prompt_propietario = f"Eres asistente Procasa para propietarios. Habla directo y claro con {nombre_prop}. Responde cualquier consulta sobre su propiedad o venta."
        respuesta = await _run_sync(
            generar_respuesta,
            [{"role": "system", "content": prompt_propietario}, *historial[-20:], {"role": "user", "content": original_message}],
            "propietario",
            telemetry_context=telemetry_context,
        )
        return await _persist_generated_outbound(
            respuesta, {"tipo": "propietario_atencion"}, intent="propietario"
        )

    # =======================================================
    # 2b. FLUJO CORREDOR DE PROPIEDADES EXTERNO
    # =======================================================
    # Si el mensaje contiene frases de corredor (canje, representación, etc.),
    # informamos que PROCASA no opera con corredores externos y cerramos el diálogo.
    broker_result = clasificar_corredor_externo(original_message)
    if broker_result["is_external_broker"]:
        respuesta_corredor = (
            "Gracias por escribirnos. Por política de la empresa, no realizamos canjes "
            "ni colaboraciones con corredores. Que tengas un buen día."
        )
        blocked = await _run_sync(
            db["leads"].update_one,
            {"phone": phone, "conversation_status": {"$ne": "BLOCKED_EXTERNAL_BROKER"}},
            {"$set": {
                "conversation_status": "BLOCKED_EXTERNAL_BROKER",
                "conversation_status_reason": broker_result["reason"],
                "conversation_status_evidence": broker_result["evidence"],
                "conversation_status_at": datetime.now(CHILE_TZ).isoformat(),
                "conversation_status_version": broker_result["version"],
            }},
        )
        if blocked.modified_count != 1:
            logger.info("[CORREDOR] Otro worker ya bloqueó la conversación; no se repite rechazo.")
            return ""
        logger.info(f"[CORREDOR] Respuesta de rechazo enviada a {phone}.")
        return await _persist_generated_outbound(
            respuesta_corredor, {"tipo": "rechazo_corredor"}, intent="corredor_externo"
        )

    # =======================================================
    # 3. ANÁLISIS PRELIMINAR DE DATOS Y EXTRACCIÓN PROACTIVA
    # =======================================================
    prospecto_actual = await _run_sync(obtener_prospecto, phone) or {}
    updates_datos = {}
    
    # A) EXTRACCIÓN PROACTIVA DE DATOS PERSONALES
    # Whitelist para evitar que el AI sobreescriba campos críticos
    allowed_updates = {"email", "rut", "nombre"}
    
    if not prospecto_actual.get("email"):
        nuevo_email = extraer_email(original_message)
        if nuevo_email: updates_datos["email"] = nuevo_email

    if not prospecto_actual.get("rut"):
        nuevo_rut = extraer_rut(original_message)
        if nuevo_rut: updates_datos["rut"] = nuevo_rut

    if not prospecto_actual.get("nombre"):
        nombre_explicito = extraer_nombre_explicito(original_message)
        if nombre_explicito:
            updates_datos["nombre"] = nombre_explicito

    # When optional visit enrichment is active, the requested field is a
    # conversational slot. A plain answer such as "Juan Pérez" is valid even
    # without an introductory phrase like "me llamo".
    if visit_data_state.get("status") == "accepted":
        requested_field = visit_data_state.get("last_requested_field")
        if requested_field == "nombre" and not prospecto_actual.get("nombre") and "nombre" not in updates_datos:
            candidate = str(original_message or "").strip()
            if re.fullmatch(r"[A-Za-zÁÉÍÓÚáéíóúÑñÜü'-]{2,}(?:\s+[A-Za-zÁÉÍÓÚáéíóúÑñÜü'-]{2,}){1,3}", candidate):
                updates_datos["nombre"] = candidate
        elif requested_field == "rut" and not prospecto_actual.get("rut") and "rut" not in updates_datos:
            captured_rut = extraer_rut(original_message)
            if captured_rut:
                updates_datos["rut"] = captured_rut
        elif requested_field == "email" and not prospecto_actual.get("email") and "email" not in updates_datos:
            captured_email = extraer_email(original_message)
            if captured_email:
                updates_datos["email"] = captured_email

    # B) EXTRACCIÓN RÁPIDA DE INTENCIÓN DE BÚSQUEDA (Heurística)
    if not prospecto_actual.get("operacion"):
        if "venta" in msg_lower or "comprar" in msg_lower: updates_datos["operacion"] = "Venta"
        elif "arriendo" in msg_lower or "arrendar" in msg_lower: updates_datos["operacion"] = "Arriendo"

    if updates_datos:
        await _run_sync(actualizar_prospecto, phone, updates_datos)
        prospecto_actual.update(updates_datos)
        await _mark_visit_data_captured(updates_datos.keys())
        await _run_sync(record_observability_event, "DATA_EXTRACTED", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
            "phone": phone,
            "extracted": updates_datos
        })

    # Optional analytics fields are captured only when volunteered by the
    # client. No question is generated for these fields in Phase 1.
    spontaneous_signals = extract_spontaneous_lead_signals(
        original_message, prospecto_actual.get("operacion")
    )
    if spontaneous_signals:
        await _run_sync(actualizar_prospecto, phone, spontaneous_signals)
        prospecto_actual.update(spontaneous_signals)
        await _run_sync(record_observability_event, "LEAD_ANALYTICS_SIGNAL_CAPTURED", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
            "fields": sorted(spontaneous_signals),
        })

    if pending_visit_data_reply in {"accepted", "declined"}:
        await _run_sync(record_observability_event, {
            "accepted": "visit_data_accepted",
            "declined": "visit_data_declined",
        }[pending_visit_data_reply], {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
        })

    # =======================================================
    # 4. ANÁLISIS DE PROPIEDAD (LINK O CÓDIGO) - VERSIÓN CORREGIDA
    # =======================================================
    propiedad = None
    nuevo_origen = None
    codigo_externo = None  # Solo para trazabilidad, no para routing si no hay match
    codigo_detectado = None
    link_operation = None

    logger.info(
        f"[PROPERTY_TRACE] origen=PRE_LINK_RESOLUTION trace={trace_id} phone={phone} "
        f"prospecto.codigo={prospecto_actual.get('codigo')} prospecto.comuna={prospecto_actual.get('comuna')}"
    )
    
    # 1. Intentar detectar Link o Código en el mensaje actual
    es_link, temp_prop, plataforma_origen, codigo_externo_raw = await _run_sync(analizar_mensaje_para_link, original_message, phone, trace_id)
    hay_url = bool(URL_RE.search(original_message))
    logger.info(
        f"[LINK_FLOW] trace={trace_id} phone={phone} hay_url={hay_url} plataforma={plataforma_origen} "
        f"temp_prop={(temp_prop.get('codigo') if temp_prop else None)} "
        f"codigo_externo_raw={codigo_externo_raw} collection={PROPERTY_COLLECTION_NAME}"
    )
    logger.info(
        f"[PROPERTY_TRACE] origen=LINK_EXTRACTOR_RESULT trace={trace_id} phone={phone} "
        f"es_link={es_link} temp_prop_codigo={(temp_prop.get('codigo') if temp_prop else None)} "
        f"temp_prop_comuna={(temp_prop.get('comuna') if temp_prop else None)}"
    )

    # 0. Resolución temprana de propiedad aunque NO haya URL.
    #    Esto usa el código ya guardado en prospecto o lo que exista en el historial.
    if not propiedad:
        db_props = await _run_sync(get_db)
        candidatos_propiedad = []

        # Código ya conocido del prospecto
        if prospecto_actual.get("codigo"):
            candidatos_propiedad.append(prospecto_actual.get("codigo"))

        # Códigos detectados en el mensaje actual o historial reciente
        for fuente in [original_message, " ".join([m.get("content", "") for m in historial[-8:]])]:
            if not fuente:
                continue
            urls_detectadas = URL_RE.findall(fuente)
            candidatos_propiedad.extend(urls_detectadas)

            cod_toctoc = extraer_codigo_toctoc_compuesto(fuente)
            if cod_toctoc:
                candidatos_propiedad.append(cod_toctoc)

            cod_int = extraer_codigo_internacional(fuente)
            if cod_int:
                candidatos_propiedad.append(cod_int)

            for cod_short in re.findall(r"\b(\d{4,6})\b", fuente):
                candidatos_propiedad.append(cod_short)

        vistos = set()
        for raw_candidate in candidatos_propiedad:
            candidate = str(raw_candidate).strip()
            if not candidate or candidate in vistos:
                continue
            vistos.add(candidate)
            prop_match = await _run_sync(_buscar_propiedad_en_universo, db_props, candidate, plataforma_origen if hay_url else None)
            if prop_match:
                propiedad = prop_match
                codigo_detectado = str(prop_match.get("codigo"))
                if not nuevo_origen:
                    nuevo_origen = "WhatsApp"
                logger.info(
                    f"[PROPERTY_RESOLVE] trace={trace_id} phone={phone} "
                    f"match_por_contexto={candidate} codigo={codigo_detectado} comuna={prop_match.get('comuna')}"
                )
                break

    if es_link and temp_prop:
        _codigo_antes = prospecto_actual.get('codigo')
        propiedad = temp_prop
        nuevo_origen = plataforma_origen or "WhatsApp"
        codigo_detectado = str(propiedad.get("codigo"))
        codigo_externo = codigo_externo_raw
        link_operation = (propiedad.get("_link_match") or {}).get("operation")
        logger.info(
            f"[PROPERTY_BEFORE] trace={trace_id} phone={phone} variable=propiedad valor_anterior={_codigo_antes}"
        )
        logger.info(
            f"[PROPERTY_AFTER] trace={trace_id} phone={phone} variable=propiedad "
            f"valor_nuevo={codigo_detectado} comuna={propiedad.get('comuna')} origen=LINK_EXTRACTOR"
        )
        await _run_sync(actualizar_prospecto, phone, {"link_detectado": True}, trace_id)
        await _run_sync(record_observability_event, "PROPERTY_RESOLVED", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
            "phone": phone,
            "property_id": codigo_detectado,
            "source": "LINK"
        })
    elif es_link and not temp_prop:
        nuevo_origen = plataforma_origen or "WhatsApp"
        codigo_externo = codigo_externo_raw
        await _run_sync(actualizar_prospecto, phone, {"link_detectado": True}, trace_id)

    # 2. Si no viene un enlace y todavía no resolvimos propiedad, intentar detectar CODIGO INTERNACIONAL (9+ dígitos)
    if not propiedad and not hay_url:
        cod_int = extraer_codigo_internacional(original_message)
        if cod_int:
            db_props = await _run_sync(get_db)
            _antes_int = prospecto_actual.get('codigo')
            propiedad = await _run_sync(_buscar_propiedad_en_universo, db_props, cod_int, plataforma_origen if hay_url else None)
            if propiedad:
                codigo_detectado = str(propiedad.get("codigo"))
                nuevo_origen = "WhatsApp (Int Code)"
                await _run_sync(record_observability_event, "PROPERTY_RESOLVED", {
                    "conversation_id": conversation_id,
                    "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
                    "phone": phone,
                    "property_id": codigo_detectado,
                    "source": "INTERNATIONAL_CODE"
                })
                logger.info(
                    f"[PROPERTY_BEFORE] trace={trace_id} phone={phone} variable=propiedad valor_anterior={_antes_int}"
                )
                logger.info(
                    f"[PROPERTY_AFTER] trace={trace_id} phone={phone} variable=propiedad "
                    f"valor_nuevo={codigo_detectado} comuna={propiedad.get('comuna')} origen=CODIGO_INTERNACIONAL"
                )

    # 3. Si no viene un enlace y seguimos sin propiedad, buscar código numérico explícito corto (4-6 dígitos)
    if not propiedad and not hay_url:
        match = re.search(r"\b(\d{4,6})\b", original_message)
        if match:
            cod = match.group(1)
            db_props = await _run_sync(get_db)
            _antes_short = prospecto_actual.get('codigo')
            propiedad = await _run_sync(_buscar_propiedad_en_universo, db_props, cod, plataforma_origen if hay_url else None)
            if propiedad:
                codigo_detectado = str(propiedad.get("codigo"))
                if not prospecto_actual.get("origen"):
                    nuevo_origen = "WhatsApp"
                await _run_sync(record_observability_event, "PROPERTY_RESOLVED", {
                    "conversation_id": conversation_id,
                    "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
                    "phone": phone,
                    "property_id": codigo_detectado,
                    "source": "SHORT_CODE"
                })
                logger.info(
                    f"[PROPERTY_BEFORE] trace={trace_id} phone={phone} variable=propiedad valor_anterior={_antes_short}"
                )
                logger.info(
                    f"[PROPERTY_AFTER] trace={trace_id} phone={phone} variable=propiedad "
                    f"valor_nuevo={codigo_detectado} comuna={propiedad.get('comuna')} origen=CODIGO_CORTO"
                )

    # Si hay enlace pero no encontramos propiedad, no heredamos código histórico.
    if hay_url and not propiedad:
        codigo_detectado = None
        await _run_sync(actualizar_prospecto, phone, {
            "origen": plataforma_origen or prospecto_actual.get("origen") or "WhatsApp",
            "link_pendiente": True,
            "codigo": None
        }, trace_id)
        logger.info(f"[LINK_FLOW] trace={trace_id} phone={phone} link sin match. Marcado link_pendiente=True")
        
        # Enviar alerta silenciosa al admin sobre el link roto o propiedad faltante
        prospecto_temporal = dict(prospecto_actual)
        prospecto_temporal["link_pendiente"] = True
        await _await_durable_handoff(
            alert_type="MissingProperty", lead_score=0, criteria=prospecto_temporal,
            last_response="", last_user_msg=original_message, full_history=historial,
            window_minutes=60, lead_type_label="Propiedad No Encontrada",
        )

        respuesta_link_pendiente = (
            "Gracias por compartir el enlace. En este momento no pude identificar la propiedad en nuestra base de datos, "
            "pero **ya he notificado internamente a nuestro administrador** para que revise el caso en detalle. "
            "Apenas actualicemos la información, un ejecutivo se pondrá en contacto contigo. "
            "Si lo prefieres, también puedes contarme qué tipo de propiedad buscas y te ayudaré con otras opciones."
        )
        return await _persist_generated_outbound(
            respuesta_link_pendiente,
            {"tipo": "link_no_encontrado", "intencion": "consulta_general", "lead_intent": LeadIntent.ASK_INFO},
            intent="consulta_general",
        )
    elif propiedad:
        # NOTA: "codigo": None if hay_url else ... → el None es filtrado por actualizar_prospecto (storage.py:224)
        # Se mantiene el campo solo por completitud lógica, no escribe None a Mongo.
        await _run_sync(actualizar_prospecto, phone, {"link_pendiente": False, "codigo": None if hay_url else prospecto_actual.get("codigo")}, trace_id)
        logger.info(f"[LINK_FLOW] trace={trace_id} phone={phone} propiedad encontrada codigo={codigo_detectado} origen={nuevo_origen}")

    # Actualizar prospecto si encontramos propiedad nueva
    if propiedad and codigo_detectado:
        prop_loc = get_prop_location(propiedad)
        prop_op = get_prop_operation(propiedad, operation_override=link_operation)
        updates_prop = {
            "ultimo_mensaje": datetime.now(CHILE_TZ).isoformat(),
            "codigo": codigo_detectado,
            "precio_uf": prop_op.get("precio_uf"),
            "comuna": prop_loc.get("comuna"),
            "tipo": prop_op.get("tipo"),
            "operacion": prop_op.get("operacion"),
            "codigo_propiedad": codigo_detectado,
            "operacion_fuente": link_operation,
            "portal_origen": plataforma_origen,
            "external_id_origen": codigo_externo,
            "origen": nuevo_origen,  # Siempre actualiza origen si viene de link
            "link_pendiente": False
        }
        logger.info(
            f"[PROPERTY_BEFORE] trace={trace_id} phone={phone} variable=prospecto.codigo "
            f"valor_anterior={prospecto_actual.get('codigo')}"
        )
        logger.info(f"[LINK_FLOW] trace={trace_id} phone={phone} actualizando prospecto.codigo={codigo_detectado} origen={nuevo_origen}")
        await _run_sync(actualizar_prospecto, phone, updates_prop, trace_id)
        logger.info(
            f"[PROPERTY_AFTER] trace={trace_id} phone={phone} variable=prospecto.codigo "
            f"valor_nuevo={codigo_detectado} comuna={propiedad.get('comuna')} origen=LINK_FLOW_FINAL"
        )

        # Registrar para anti-repetición en RAG
        await _run_sync(registrar_propiedades_vistas, phone, [codigo_detectado])

        # Derivación pasiva al equipo cuando la propiedad ya quedó resuelta.
        # No bloquea la respuesta del bot: se ejecuta en segundo plano.
        if lead_doc_full.get("_id"):
            asyncio.create_task(
                asyncio.to_thread(
                    LeadProcessingService.process_lead,
                    lead_doc_full.get("_id"),
                    False,
                    False
                )
            )

    # === CORRECCIÓN: Guardar código externo aunque no esté en DB ===
    if codigo_externo and propiedad:
        ext_updates = {"origen": nuevo_origen}
        if plataforma_origen == "Yapo":
            ext_updates["codigo_yapo"] = codigo_externo
        elif plataforma_origen in ["MercadoLibre", "PortalInmobiliario"]:
            ext_updates["codigo_mercadolibre"] = codigo_externo
        logger.info(f"[LINK_FLOW] trace={trace_id} phone={phone} sobrescritura externa={ext_updates}")
        await _run_sync(actualizar_prospecto, phone, ext_updates, trace_id)

    # --- NOTIFICACIÓN POR PROPIEDAD DESCONOCIDA ---
    if es_link and not propiedad and codigo_externo:
        # Si es un link pero no encontramos propiedad, notificamos al Admin (Pablo Galleguillos)
        admin_phone = "56983219804" # Pablo Galleguillos
        admin_msg = (
            f"🚨 *Propiedad No Encontrada*\n\n"
            f"El cliente {prospecto_actual.get('nombre', 'Desconocido')} ({phone}) "
            f"envió un link de {plataforma_origen or 'Portal'} con código `{codigo_externo}`, "
            f"pero no existe en `universo_cartera`.\n\n"
            f"El lead quedó sin propiedad confirmada. Favor revisar el enlace o actualizar la base."
        )
        await _await_durable_handoff(
            alert_type="MissingProperty", lead_score=0,
            criteria={
                "nombre": "Admin Pablo",
                "codigo_faltante": codigo_externo,
                "link_pendiente": True,
                "codigo": "",
            },
            last_response=admin_msg, last_user_msg=original_message,
            full_history=[], window_minutes=120,
            lead_type_label="PROPIEDAD FALTANTE", alert_phone=admin_phone,
        )
        logger.info(f"📢 Alerta de propiedad faltante ({codigo_externo}) enviada al administrador.")

    # =======================================================
    # 5. PREPARACIÓN DE MESSAGES PARA GROK
    # =======================================================
    messages_para_grok = []
    
    # 1. AGREGAMOS EL PROMPT DE SISTEMA ESTRICTO
    messages_para_grok.append({"role": "system", "content": SYSTEM_PROMPT_PROSPECTO})
    
    # 2. Agregamos el historial reciente
    for m in historial[-6:]: # Usamos los últimos 6 para contexto
        messages_para_grok.append(m)

    system_parts = []

    # --- CONTEXTO EXTRA PARA URLs ---
    urls_contexto = extraer_contexto_urls(original_message)
    if urls_contexto:
        system_parts.append(
            "[CONTEXTO DE ENLACES DETECTADOS]\n"
            f"{json.dumps(urls_contexto, ensure_ascii=False)}\n"
            "INSTRUCCIÓN: si el usuario envía enlaces, usa este contexto explícito para identificar plataforma/código "
            "y no asumas propiedades que no estén confirmadas por la base de datos."
        )
    
    # --- CONTEXTO 1: ESTADO DE DATOS PERSONALES ---
    # A visit lead is actionable with the WhatsApp identity already available.
    # Name, RUT and email are optional enrichment, never a gate for alerting.
    system_parts.append("""
    [COORDINACIÓN DE VISITA]
    El teléfono canónico ya está disponible por WhatsApp. Nunca solicites teléfono,
    celular, WhatsApp ni número de contacto. Nombre, RUT y correo son opcionales:
    no los presentes como requisito ni los solicites para la primera coordinación.
    Si corresponde hacer una pregunta de visita, formula solo una: día o rango horario
    preferido. No confirmes ni agendes una visita; el ejecutivo confirma disponibilidad.
    """)

    # --- CONTEXTO 1.5: EVALUAR EJECUTIVO HISTÓRICO EFECTIVO ---
    current_exec = lead_doc_full.get("ejecutivo_asignado") or prospecto_actual.get("ejecutivo")
    effective_exec = None
    if current_exec and current_exec not in ["No Asignado", "Sin Asignar", None, ""]:
        # Verificamos que siga disponible
        from .lead_router import get_active_executive
        comuna_lead = prospecto_actual.get("comuna") or (propiedad.get("comuna") if propiedad else "")
        effective_exec = get_active_executive(current_exec, comuna_lead)
        # Si fue reasignado, actualizamos tempranamente en la BD y en las variables
        if effective_exec != current_exec:
            logger.info(f"[CORE] Reasignación temprana detectada de {current_exec} a {effective_exec} por indisponibilidad.")
            await _run_sync(
                db["leads"].update_one,
                {"phone": phone},
                {"$set": {"ejecutivo_asignado": effective_exec, "prospecto.ejecutivo": effective_exec}}
            )
            lead_doc_full["ejecutivo_asignado"] = effective_exec
            prospecto_actual["ejecutivo"] = effective_exec

    # --- CONTEXTO 2: INFORMACIÓN DE PROPIEDADES (PRIORIDAD: ESPECÍFICA > BÚSQUEDA) ---

    alternatives_requested_now = alternative_requested(original_message)
    rejection_without_alternative = property_rejected(original_message) and not alternatives_requested_now
    rag_search_state = dict(prospecto_actual.get("rag_search_state") or {})
    stored_rag_criteria = dict(rag_search_state.get("criteria") or {})
    extracted_rag_filters, _ = extraer_filtros_estructurados(original_message)
    explicit_rag_criteria = {}
    if extracted_rag_filters.get("operacion"):
        explicit_rag_criteria["operacion"] = extracted_rag_filters["operacion"]
    if extracted_rag_filters.get("tipo"):
        explicit_rag_criteria["tipo"] = extracted_rag_filters["tipo"]
    if extracted_rag_filters.get("comunas"):
        explicit_rag_criteria["comuna"] = ", ".join(extracted_rag_filters["comunas"])
        explicit_rag_criteria["comunas"] = list(extracted_rag_filters["comunas"])
    if extracted_rag_filters.get("dormitorios"):
        explicit_rag_criteria["dormitorios"] = extracted_rag_filters["dormitorios"]
    budget = extracted_rag_filters.get("precio_uf_max") or extracted_rag_filters.get("precio_clp_max")
    if budget:
        explicit_rag_criteria["presupuesto"] = budget
    if explicit_rag_criteria:
        stored_rag_criteria.update(explicit_rag_criteria)
        rag_search_state = await _run_sync(update_rag_search_state, phone, {
            "criteria": stored_rag_criteria,
            "criteria_updated_at": datetime.now(CHILE_TZ).isoformat(),
        })
        prospecto_actual["rag_search_state"] = rag_search_state

    # A property's original attributes are not copied as customer preferences.
    # Operation may still be used as transactional context for the existing RAG
    # query, while commune/budget/type/bedrooms come from explicit criteria.
    criterios_rag = {
        "operacion": stored_rag_criteria.get("operacion") or prospecto_actual.get("operacion"),
        "tipo": stored_rag_criteria.get("tipo"),
        "comuna": stored_rag_criteria.get("comuna"),
        "dormitorios": stored_rag_criteria.get("dormitorios"),
        "presupuesto": stored_rag_criteria.get("presupuesto"),
    }

    # CASO A: Propiedad Específica (Link o Código), salvo que el cliente pida
    # alternativas explícitamente.
    if propiedad and not alternatives_requested_now and not rejection_without_alternative and alternative_reply != "accepted":
        ficha_texto = formatear_ficha_tecnica(propiedad, lead_executive=effective_exec)
        system_parts.append(f"""
        [DATOS OFICIALES DE LA PROPIEDAD ACTIVA]
        {ficha_texto}
        """)

    # CASO B: Búsqueda / RAG (Solo si no hay propiedad específica activa)
    else:
        # LÓGICA RAG: la propiedad activa no impide buscar alternativas cuando
        # el cliente las solicita. Un rechazo sin solicitud solo ofrece ayuda.
        is_search_intent = any(x in msg_lower for x in ["busco", "otra", "tienes", "opciones", "más"])
        is_initial_search = len(historial) <= 6 # Heurística para etapas tempranas
        rag_state = (prospecto_actual.get("rag_filter_relaxation") or {})
        relaxation_accepted = filter_relaxation_accepted(
            original_message,
            offer_pending=rag_state.get("status") == "offered" and alternative_reply == "none",
        )
        if relaxation_accepted:
            await _run_sync(actualizar_prospecto, phone, {
                "rag_filter_relaxation": {**rag_state, "status": "accepted", "accepted_at": datetime.now(CHILE_TZ).isoformat()}
            })
            await _run_sync(record_observability_event, "rag_filter_relaxation_accepted", {
                "conversation_id": conversation_id,
                "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
            })
        allow_rag = not rejection_without_alternative and alternative_reply != "declined" and (
            alternatives_requested_now or alternative_reply == "accepted" or is_search_intent or is_initial_search or relaxation_accepted
        )

        if criterios_rag["operacion"] and criterios_rag["comuna"] and allow_rag:
            
            # --- LÓGICA CLAVE: ANTI-REPETICIÓN Y LÍMITE ---
            codigos_vistos = obtener_propiedades_vistas(phone)
            if propiedad and propiedad.get("codigo"):
                codigos_vistos = list(set(codigos_vistos + [str(propiedad.get("codigo"))]))

            await _run_sync(record_observability_event, "rag_search_started", {
                "conversation_id": conversation_id,
                "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
                "property_id": prospecto_actual.get("codigo"),
                "operation": criterios_rag.get("operacion"),
                "strict_filters": not relaxation_accepted,
            })
            
            # Buscamos excluyendo lo visto y limitando a 3 (o el límite que se defina)
            # USAMOS BÚSQUEDA SEMÁNTICA (HÍBRIDA) EN VEZ DE LA SIMPLE
            resultados_rag = buscar_semanticamente(
                original_message, 
                exclude_codes=codigos_vistos, 
                limit=3,
                criterios_estructurados=criterios_rag,
                allow_filter_relaxation=relaxation_accepted,
            )

            await _run_sync(record_observability_event, "rag_search_completed", {
                "conversation_id": conversation_id,
                "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
                "result_count": len(resultados_rag or []),
                "strict_filters": not relaxation_accepted,
            })
            
            texto_rag = formatear_resultados_texto(resultados_rag)
            
            if resultados_rag:
                # --- CORRECCIÓN: ALMACENAMIENTO DE VISTOS ---
                # Usamos p.get() para seguridad y forzamos str() para que coincida con MongoDB
                nuevos_codigos = [str(p.get("codigo")) for p in resultados_rag if p.get("codigo")]
                
                # Guardamos inmediatamente en Mongo para que la próxima búsqueda los excluya
                if nuevos_codigos:
                    await _run_sync(registrar_propiedades_vistas, phone, nuevos_codigos)
                    logger.info(f"[RAG] Propiedades registradas como vistas para {phone}: {nuevos_codigos}")
                # ---------------------------------------------
                
                system_parts.append(f"""
                [SISTEMA DE BÚSQUEDA - NUEVAS OPCIONES]
                El cliente busca: {json.dumps(criterios_rag, ensure_ascii=False)}.
                HEMOS ENCONTRADO ESTAS {len(nuevos_codigos)} OPCIONES NUEVAS Y FORMATEADAS (Usar el formato de párrafo y Link)
                {texto_rag}
                INSTRUCCIÓN: Ofrece estas opciones en relato natural, usa el formato de párrafo y el Link proporcionado. Pregunta cuál le gustaría visitar.
                """)
                await _run_sync(record_observability_event, "rag_alternative_offered", {
                    "conversation_id": conversation_id,
                    "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
                    "result_count": len(resultados_rag),
                })
                if alternative_reply == "accepted":
                    await _run_sync(update_rag_alternative_offer_state, phone, {
                        "status": "consumed", "consumed_at": datetime.now(CHILE_TZ).isoformat(),
                    })
            else:
                if not relaxation_accepted:
                    await _run_sync(actualizar_prospecto, phone, {
                        "rag_filter_relaxation": {
                            "status": "offered",
                            "original_filters": criterios_rag,
                            "offered_at": datetime.now(CHILE_TZ).isoformat(),
                        }
                    })
                    await _run_sync(record_observability_event, "rag_filter_relaxation_offered", {
                        "conversation_id": conversation_id,
                        "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
                        "criteria_fields": sorted(k for k, value in criterios_rag.items() if value),
                    })
                system_parts.append(f"""
                [SISTEMA DE BÚSQUEDA]
                Buscamos con: {json.dumps(criterios_rag, ensure_ascii=False)} y NO hay más resultados nuevos con esos filtros.
                INSTRUCCIÓN: Informa que no hay opciones exactas y pregunta si quiere ampliar un poco la búsqueda. No envíes propiedades todavía.
                """)
                if alternative_reply == "accepted":
                    await _run_sync(update_rag_alternative_offer_state, phone, {
                        "status": "consumed", "consumed_at": datetime.now(CHILE_TZ).isoformat(),
                    })
        
        # Si faltan datos para buscar y parece que quiere buscar
        elif any(x in msg_lower for x in ["busco", "necesito", "quiero", "tienen"]):
            faltan_rag = []
            if not criterios_rag["operacion"]: faltan_rag.append("si es Venta o Arriendo")
            if not criterios_rag["comuna"]: faltan_rag.append("la Comuna")
            
            system_parts.append(f"""
            [ASISTENTE DE BÚSQUEDA]
            Faltan datos para buscar propiedades: {', '.join(faltan_rag)}.
            INSTRUCCIÓN: Pregunta amablemente por estos datos.
            """)
        elif rejection_without_alternative:
            property_id = prospecto_actual.get("codigo") or (propiedad or {}).get("codigo")
            if property_id:
                await _run_sync(registrar_propiedades_vistas, phone, [str(property_id)])
            await _run_sync(update_rag_alternative_offer_state, phone, {
                "status": "offered",
                "property_id": str(property_id) if property_id else None,
                "offered_at": datetime.now(CHILE_TZ).isoformat(),
                "conversation_id": conversation_id,
            })
            await _run_sync(record_observability_event, "rag_alternative_offered", {
                "conversation_id": conversation_id,
                "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
                "property_id": str(property_id) if property_id else None,
                "result_count": 0,
                "offer_only": True,
            })
            system_parts.append("""
            [PROPIEDAD DESCARTADA]
            El cliente no quedó conforme con la propiedad y no pidió alternativas explícitamente.
            Responde brevemente ofreciendo buscar opciones que se ajusten mejor. No envíes propiedades todavía.
            """)

    # Insertar Contexto Dinámico al final del System Prompt
    if system_parts:
        full_context = "\n\n".join(system_parts)
        messages_para_grok.append({"role": "system", "content": full_context})

    # Mensaje final del usuario
    messages_para_grok.append({"role": "user", "content": original_message})

    # =======================================================
    # 6. RESPUESTA CON GROK (Generación + Extracción)
    # =======================================================
    from .storage import get_pending_response
    pending_visit_before = await _run_sync(get_pending_response, phone, "VISIT_CONFIRMATION")
    # CORRECCIÓN: Recargar prospecto_actual para reflejar el codigo ya guardado (16479)
    # y para que el log [DEEPSEEK PROPERTY_PAYLOAD] sea exacto y no muestre el código anterior.
    prospecto_actual = await _run_sync(obtener_prospecto, phone) or {}
    # Canonical identity for the conversational pipeline.  Historical nested
    # phone variants remain read-compatible but are not used as a primary source.
    prospecto_actual["phone"] = lead_doc_full.get("phone") or phone
    current_property_id = str(prospecto_actual.get("codigo") or "") or None
    state_property_id = str(visit_data_state.get("property_id") or "") or None
    if (
        current_property_id
        and state_property_id
        and current_property_id != state_property_id
        and visit_data_state.get("status") in {"declined", "completed"}
    ):
        # A decline belongs to one property/intention cycle, not to the lead
        # forever. Previously captured fields remain known and are not requested again.
        visit_data_state = await _run_sync(update_visit_data_state, phone, {
            "status": "not_offered",
            "cycle_id": str(uuid.uuid4()),
            "property_id": current_property_id,
            "cycle_started_at": datetime.now(CHILE_TZ).isoformat(),
            "offered_at": "",
            "declined_at": "",
            "declined_property_id": "",
        })
    logger.info(
        f"[PROPERTY_TRACE] origen=PRE_DEEPSEEK trace={trace_id} phone={phone} "
        f"prospecto.codigo={prospecto_actual.get('codigo')} prospecto.comuna={prospecto_actual.get('comuna')}"
    )

    # Campos que el modelo IA tiene PROHIBIDO sobrescribir (datos resueltos por el sistema)
    _CAMPOS_BLOQUEADOS_IA = {
        "codigo", "comuna", "tipo", "operacion", "precio_uf", "link_detectado",
        "link_pendiente", "origen", "codigo_yapo", "codigo_mercadolibre",
        "phone", "telefono", "celular", "whatsapp", "numero",
    }
    _CAMPOS_PERMITIDOS_IA = {
        "nombre", "rut", "email", "search_duration_bucket",
        "financing_status", "rental_docs_readiness",
    }
    _VALORES_ANALITICA = {
        "search_duration_bucket": {"just_started", "lt_1_month", "1_3_months", "3_6_months", "gt_6_months", "unknown"},
        "financing_status": {"preapproved", "under_evaluation", "needs_financing", "cash", "unknown"},
        "rental_docs_readiness": {"ready", "partially_ready", "not_ready", "unknown"},
    }

    try:
        llm_context = {
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
        }
        if telemetry_context:
            llm_context.update({k: v for k, v in telemetry_context.items() if v is not None})
        resultado_grok = await _run_sync(
            generar_respuesta_estructurada,
            messages_para_grok,
            prospecto_actual,
            llm_context,
        )

        intencion = resultado_grok["intencion"]
        respuesta = resultado_grok["respuesta_bot"]
        datos_extraidos = resultado_grok.get("datos_extraidos", {})
        operacion_contextual = str(
            prospecto_actual.get("operacion") or ""
        ).casefold()

        # Guardar nuevos datos detectados por IA — SOLO campos permitidos
        if datos_extraidos:
            datos_seguros = {
                k: v for k, v in datos_extraidos.items()
                if k in _CAMPOS_PERMITIDOS_IA
                and (k not in _VALORES_ANALITICA or v in _VALORES_ANALITICA[k])
                and not (
                    k == "financing_status"
                    and operacion_contextual not in {"venta", "comprar", "compra"}
                )
                and not (
                    k == "rental_docs_readiness"
                    and operacion_contextual not in {"arriendo", "arrendar", "alquilar", "alquiler"}
                )
            }
            datos_bloqueados = {k: v for k, v in datos_extraidos.items() if k in _CAMPOS_BLOQUEADOS_IA}
            if datos_bloqueados:
                logger.warning(
                    f"[PROPERTY_GUARD] trace={trace_id} phone={phone} "
                    f"IA intentó sobrescribir campos bloqueados: {datos_bloqueados} — BLOQUEADO"
                )
            if datos_seguros:
                logger.info(
                    f"[PROPERTY_TRACE] origen=DATOS_EXTRAIDOS_IA trace={trace_id} phone={phone} "
                    f"datos_seguros={datos_seguros}"
                )
                await _run_sync(actualizar_prospecto, phone, datos_seguros)
                await _mark_visit_data_captured(datos_seguros.keys())
    except Exception as e:
        logger.error(f"Error Grok: {e}")
        intencion = "consulta_general"
        respuesta = "Disculpa, tengo un problema técnico momentáneo."

    # =======================================================
    # 7. EXCEPCIÓN: FORZAR FICHA (RESPALDO ORIGINAL)
    # =======================================================
    if propiedad and "ficha" in original_message.lower():
         ficha_completa = formatear_ficha_tecnica(propiedad, lead_executive=effective_exec)
         respuesta = f"Aquí tienes el resumen técnico completo:\n\n{ficha_completa}"

    if rejection_without_alternative:
        respuesta = "Puedo buscarte alternativas que se ajusten mejor a lo que necesitas."

    # =======================================================
    # 8. POST-PROCESO DE EMAIL (RESPALDO ORIGINAL)
    # =======================================================
    # Esto es redundante con la extracción proactiva, pero lo dejamos como seguro
    if not prospecto_actual.get("email"):
        email_detectado = extraer_email(original_message)
        if email_detectado:
             await _run_sync(actualizar_prospecto, phone, {"email": email_detectado.lower()})

    # ── SET VISIT CONFIRMATION IF BOT ASKED ABOUT VISITING ──
    # After the AI generates a response, check if it asked a visit question
    # and set the pending confirmation state accordingly.
    from .storage import get_pending_response, resolve_pending_response, set_pending_response
    property_code = (prospecto_actual or {}).get("codigo") or ""
    # These helpers use synchronous PyMongo. process_user_message runs inside
    # an asyncio loop, so every access must be delegated off that loop.
    has_pending_confirmation = bool(
        await _run_sync(get_pending_response, phone, "VISIT_CONFIRMATION")
    )
    if not has_pending_confirmation and property_code and intencion in ("consulta_general", "agendar_visita"):
        resp_low = respuesta.lower()
        if any(p in resp_low for p in ["gustaría", "quisieras", "quieres", "coordin", "agendar", "visita", "conocer", "conozcas"]):
            prompt_suffixes = ["?", "!", ""]
            if any(resp_low.strip().endswith(s) for s in prompt_suffixes):
                try:
                    await _run_sync(
                        set_pending_response, phone, "VISIT_CONFIRMATION",
                        property_code, conversation_id,
                    )
                    logger.info("[VISIT_CONFIRM] Estado de confirmacion guardado para propiedad %s", property_code)
                except Exception as e:
                    logger.warning("[VISIT_CONFIRM] Error guardando estado: %s", e)

    # ── VISIT CONFIRMATION STATE (INTERPRET) ──
    # If there's a pending VISIT_CONFIRMATION response, interpret short
    # affirmatives as visit requests regardless of what the AI classified.
    pending = await _run_sync(get_pending_response, phone, "VISIT_CONFIRMATION")
    if pending:
        msg_l = (original_message or "").lower().strip()
        negative_terms = ["no", "no ", "no," "no.", "no gracias", "no, gracias", "no quiero",
                          "por ahora no", "mas adelante", "más adelante", "solo estoy consultando",
                          "solo consulto", "despues", "después", "no me interesa"]
        affirmative_terms = ["sí", "si", "sí, me encantaría", "si me encantaría", "sí me encantaría",
                            "claro", "por supuesto", "perfecto", "me encantaría", "me gustaría",
                            "quiero verla", "quiero verlo", "mañana podría", "mañana puedo",
                            "agendemos", "coordinemos", "dale", "obvio", "ya", "sí quiero",
                            "si quiero", "encantado", "encantada"]

        # Check negative first (explicit "no")
        is_negative = False
        for t in negative_terms:
            if msg_l == t or msg_l.startswith(t):
                is_negative = True
                break

        if is_negative:
            await _run_sync(resolve_pending_response, phone, "rejected")
            logger.info("[VISIT_CONFIRM] Pending response %s rejected by user: %s", pending.get("type"), msg_l)

        else:
            # Check if response is a short affirmative or topic change
            is_affirmative = False
            for t in affirmative_terms:
                if t in msg_l:
                    is_affirmative = True
                    break

            topic_change_terms = ["precio", "cuánto", "cuanto", "gasto", "gastos",
                                  "comunes", "mascota", "estacionamiento", "bodega",
                                  "como es", "cómo es", "metros", "tamaño", "años",
                                  "antigüedad", "escritura", "crédito", "hipotecario"]
            is_topic_change = any(t in msg_l for t in topic_change_terms)

            if is_affirmative and not is_topic_change:
                intencion = "agendar_visita"
                await _run_sync(resolve_pending_response, phone, "confirmed")
                logger.info("[VISIT_CONFIRM] Pending response %s confirmed by user: %s", pending.get("type"), msg_l)
            elif is_topic_change:
                logger.info("[VISIT_CONFIRM] Pending response %s topic changed by user: %s", pending.get("type"), msg_l)
                # Keep pending for one more turn if it could still turn into visit
            else:
                # Non-affirmative, non-negative, non-topic-change → leave pending
                logger.info("[VISIT_CONFIRM] Pending response %s still waiting: %s", pending.get("type"), msg_l)

    # --- GUARDRAIL DE INTENCIÓN (REGLAS DETERMINÍSTICAS) ---
    msg_l = (original_message or "").lower()
    visit_terms = [
        "visita", "visitar", "ir a ver", "ver la propiedad", "verlo", "verla",
        "disponible", "disponibilidad", "fin de semana", "mañana", "pasado mañana",
        "agendar", "coordinar visita", "conocer la propiedad"
    ]
    contact_terms = [
        "llamar", "llamen", "llamada", "contactar", "contacto", "ejecutivo",
        "asesor", "humano", "supervisor", "gerente"
    ]

    if intencion == "consulta_general":
        if should_offer_visit_data(
            original_message,
            intencion,
            pending_visit_confirmation=bool(pending_visit_before),
            # Intent detection is independent from whether enrichment was
            # previously declined; a later explicit visit request still
            # deserves a handoff.
            visit_data_state={},
        ):
            intencion = "agendar_visita"
        elif any(t in msg_l for t in contact_terms):
            intencion = "contacto_directo"

    # A broad model label is not sufficient to request personal data or trigger
    # a visit handoff. Operational intent must pass the deterministic layer.
    visit_intent_clear = should_offer_visit_data(
        original_message,
        intencion,
        pending_visit_confirmation=bool(pending_visit_before),
        visit_data_state={},
    )
    if intencion == "agendar_visita" and not visit_intent_clear:
        intencion = "consulta_general"
    elif visit_intent_clear:
        intencion = "agendar_visita"

    # --- NUEVA LÓGICA DE INTENCIÓN (ENTERPRISE) ---
    intent_map = {
        "agendar_visita": LeadIntent.ASK_VISIT,
        "contacto_directo": LeadIntent.ASK_CONTACT,
        "escalado_urgente": LeadIntent.ASK_INFO, # Fallback a info + alerta
        "consulta_general": LeadIntent.ASK_INFO
    }
    
    selected_intent = intent_map.get(intencion, LeadIntent.OTHER)
    await _run_sync(CrmService.update_intent, phone, selected_intent, actor="bot")

    # =======================================================
    # 9. ENVÍO DE ALERTAS...
    # =======================================================
    metadata_tipo = {"tipo": "respuesta_general", "intencion": intencion, "lead_intent": selected_intent}
    prospecto_actual = await _run_sync(obtener_prospecto, phone) or {} # Recargamos prospecto para lead score
    lead_doc = await _run_sync(CrmService.get_lead, phone) or {} # Obtenemos documento completo para score
    lead_score = await _run_sync(CrmService.calculate_score, lead_doc)
    await _run_sync(record_observability_event, "INTENT_DETECTED", {
        "conversation_id": conversation_id,
        "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
        "phone": phone,
        "intent": intencion,
        "lead_intent": str(selected_intent),
        "score": lead_score
    })

    if visit_intent_clear:
        await _run_sync(record_observability_event, "visit_intent_detected", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
            "property_id": prospecto_actual.get("codigo"),
            "operation": prospecto_actual.get("operacion"),
            "intent_strength": "explicit",
        })

    # CORRECCIÓN DE TIEMPOS: 60 minutos para evitar spam de correo
    if intencion == "escalado_urgente":
        await _run_sync(record_observability_event, "ALERT_TRIGGERED", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
            "phone": phone,
            "alert_type": "EscaladoUrgente"
        })
        await _await_durable_handoff(
            alert_type="EscaladoUrgente", lead_score=lead_score, criteria=prospecto_actual,
            last_response=respuesta, last_user_msg=original_message, full_history=historial,
            window_minutes=60, lead_type_label="ESCALADO URGENTE",
        )
        metadata_tipo = {"tipo": "escalado_urgente", "intencion": intencion}

    elif intencion == "agendar_visita":
        await _run_sync(record_observability_event, "ALERT_TRIGGERED", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
            "phone": phone,
            "alert_type": "InteresVisita"
        })
        await _await_durable_handoff(
            alert_type="InteresVisita", lead_score=lead_score, criteria=prospecto_actual,
            last_response=respuesta, last_user_msg=original_message, full_history=historial,
            window_minutes=60, lead_type_label="Interés de Visita",
        )
        metadata_tipo = {"tipo": "gestion_visita", "intencion": intencion}

    elif intencion == "contacto_directo":
        await _run_sync(record_observability_event, "ALERT_TRIGGERED", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
            "phone": phone,
            "alert_type": "SolicitudContacto"
        })
        await _await_durable_handoff(
            alert_type="SolicitudContacto", lead_score=lead_score, criteria=prospecto_actual,
            last_response=respuesta, last_user_msg=original_message, full_history=historial,
            window_minutes=60, lead_type_label="Solicitud de Contacto",
        )
        metadata_tipo = {"tipo": "contacto_directo", "intencion": intencion}

    # The handoff is independent from enrichment. Offer data only after the
    # explicit visit signal and keep the offer optional/non-blocking.
    if visit_intent_clear and visit_data_state.get("status") not in {"offered", "accepted", "declined", "completed"}:
        optional_offer = (
            "Si quieres, puedo dejar adelantados tus datos para que el ejecutivo encargado "
            "pueda coordinar la visita más rápido. Es opcional; si prefieres, puedes entregárselos directamente."
        )
        if "dejar adelantados" not in respuesta.lower():
            respuesta = f"{respuesta.rstrip()}\n\n{optional_offer}".strip()
        visit_data_state = await _run_sync(update_visit_data_state, phone, {
            "status": "offered",
            "offered_at": datetime.now(CHILE_TZ).isoformat(),
            "property_id": current_property_id or prospecto_actual.get("codigo"),
            "cycle_id": visit_data_state.get("cycle_id") or str(uuid.uuid4()),
            "conversation_id": conversation_id,
        })
        await _run_sync(record_observability_event, "visit_data_offered", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
            "property_id": prospecto_actual.get("codigo"),
        })
    elif visit_data_state.get("status") == "declined":
        # A decline closes this capture cycle; never let the model append the
        # next personal-field question after the client said no.
        respuesta = visit_data_declined_response()
    elif visit_data_state.get("status") == "accepted":
        missing_fields = visit_data_fields_missing(visit_data_state, prospecto_actual)
        if missing_fields:
            next_field = missing_fields[0]
            requested = set(visit_data_state.get("requested_fields") or [])
            if next_field not in requested:
                respuesta = f"{respuesta.rstrip()}\n\n{build_visit_data_prompt(next_field)}".strip()
                visit_data_state = await _run_sync(update_visit_data_state, phone, {
                    "requested_fields": sorted(requested | {next_field}),
                    "last_requested_field": next_field,
                })

    # Common final guard for every generation path, including structured JSON
    # recovery and the explicit ficha response.
    if outbound_phone_request(respuesta):
        await _run_sync(record_observability_event, "phone_request_blocked", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
        })
        respuesta = safe_phone_free_response(respuesta)
    if outbound_unconfirmed_visit_claim(respuesta):
        await _run_sync(record_observability_event, "visit_claim_blocked", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
        })
        respuesta = safe_visit_claim_free_response(respuesta)
    previous_bot_responses = [
        str(item.get("content") or "") for item in historial
        if item.get("role") == "assistant"
    ]
    if is_substantial_duplicate(respuesta, previous_bot_responses[-5:]):
        await _run_sync(record_observability_event, "bot_response_duplicate_blocked", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
        })
        respuesta = duplicate_response_fallback(respuesta)

    # =======================================================
    # 10. GUARDAR Y RETORNAR (COMPLETO)
    # =======================================================
    logger.info(
        "[MONGO_SAVE_SIZE] respuesta_len=%s",
        len(respuesta or "")
    )
    return await _persist_generated_outbound(
        respuesta,
        {**metadata_tipo, "lead_intent": selected_intent},
        intent=intencion,
        lead_doc=lead_doc,
    )


def process_user_message_sync(phone: str, message: str, telemetry_context: dict | None = None) -> str:
    """Sync wrapper for process_user_message — used from threadpool workers.
    
    Runs the full async process_user_message in a fresh event loop.
    """
    import asyncio
    
    async def _run():
        from chatbot.core import process_user_message
        return await process_user_message(
            phone, message, is_from_me=False, telemetry_context=telemetry_context,
        )
    
    return asyncio.run(_run())
    return respuesta
