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
    ensure_conversation_id
)
from .crm_service import CrmService
from .constants import PipelineStage, InteractionType, LeadIntent, CHILE_TZ

from .grok_client import generar_respuesta, generar_respuesta_estructurada
from .link_extractor import analizar_mensaje_para_link, extraer_codigo_internacional, extraer_contexto_urls, URL_RE
from .utils import extraer_rut, extraer_email, safe_int_conversion, extraer_nombre_explicito
from .utils import parse_bool
from .alert_service import send_alert_once
from .classifier import es_propietario, es_corredor_externo
from .processing_service import LeadProcessingService
from .property_lookup import PROPERTY_COLLECTION_NAME, find_property_by_any_identifier, get_prop_location, get_prop_operation

# RAG IMPORT
from .rag import buscar_propiedades, formatear_resultados_texto, buscar_semanticamente
# Importamos el prompt maestro con las reglas estrictas (No horarios, no inventar)
from .prompts import SYSTEM_PROMPT_PROSPECTO 

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
def formatear_ficha_tecnica(propiedad):
    """
    Formato estándar para inyectar en el prompt cuando hay una propiedad específica.
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
    precio_uf_str  = str(precio_uf_raw) if precio_uf_raw is not None else "N/D"
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

    # ── Ejecutivo ─────────────────────────────────────────────────────────────
    ejecutivo = estado.get("ejecutivo") or propiedad.get("ejecutivo") or "N/D"

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
    lineas.append(f"Ejecutivo a cargo:  {ejecutivo}")
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


async def process_user_message(phone: str, message: str, is_from_me: bool = False) -> str:
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
    prospecto_actual = lead_doc_full.get("prospecto", {})
    conversation_id = lead_doc_full.get("conversation_id") or prospecto_actual.get("conversation_id")
    if not conversation_id:
        conversation_id = await _run_sync(ensure_conversation_id, phone)
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
            "propietario"
        )
        await _run_sync(guardar_mensaje, phone, "assistant", respuesta, {"tipo": "propietario_atencion"})
        return respuesta

    # =======================================================
    # 2b. FLUJO CORREDOR DE PROPIEDADES EXTERNO
    # =======================================================
    # Si el mensaje contiene frases de corredor (canje, representación, etc.),
    # informamos que PROCASA no opera con corredores externos y cerramos el diálogo.
    if es_corredor_externo(original_message):
        respuesta_corredor = (
            "Estimado/a, muchas gracias por contactarnos. "
            "PROCASA no realiza operaciones de canje ni trabaja con corredores externos. "
            "Si usted es cliente final y está buscando una propiedad, con gusto lo atendemos. "
            "Que tenga un excelente día."
        )
        logger.info(f"[CORREDOR] Respuesta de rechazo enviada a {phone}.")
        await _run_sync(guardar_mensaje, phone, "assistant", respuesta_corredor, {"tipo": "rechazo_corredor"})
        return respuesta_corredor

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

    # B) EXTRACCIÓN RÁPIDA DE INTENCIÓN DE BÚSQUEDA (Heurística)
    if not prospecto_actual.get("operacion"):
        if "venta" in msg_lower or "comprar" in msg_lower: updates_datos["operacion"] = "Venta"
        elif "arriendo" in msg_lower or "arrendar" in msg_lower: updates_datos["operacion"] = "Arriendo"

    if updates_datos:
        await _run_sync(actualizar_prospecto, phone, updates_datos)
        prospecto_actual.update(updates_datos)
        await _run_sync(record_observability_event, "DATA_EXTRACTED", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc_full.get("_id")) if lead_doc_full.get("_id") else None,
            "phone": phone,
            "extracted": updates_datos
        })

    # =======================================================
    # 4. ANÁLISIS DE PROPIEDAD (LINK O CÓDIGO) - VERSIÓN CORREGIDA
    # =======================================================
    propiedad = None
    nuevo_origen = None
    codigo_externo = None  # Solo para trazabilidad, no para routing si no hay match
    codigo_detectado = None

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
        asyncio.create_task(send_alert_once(phone=phone, lead_type="MissingProperty", lead_score=0,
                        criteria=prospecto_temporal, last_response="", last_user_msg=original_message,
                        full_history=historial, window_minutes=60, lead_type_label="Propiedad No Encontrada"))

        respuesta_link_pendiente = (
            "Gracias por compartir el enlace. No pude identificar la propiedad en este momento. "
            "Si quieres, envíame el enlace nuevamente o dime qué tipo de propiedad buscas y te ayudo."
        )
        await _run_sync(guardar_mensaje, phone, "assistant", respuesta_link_pendiente, {
            "tipo": "link_no_encontrado",
            "intencion": "consulta_general",
            "lead_intent": LeadIntent.ASK_INFO
        })
        return respuesta_link_pendiente
    elif propiedad:
        # NOTA: "codigo": None if hay_url else ... → el None es filtrado por actualizar_prospecto (storage.py:224)
        # Se mantiene el campo solo por completitud lógica, no escribe None a Mongo.
        await _run_sync(actualizar_prospecto, phone, {"link_pendiente": False, "codigo": None if hay_url else prospecto_actual.get("codigo")}, trace_id)
        logger.info(f"[LINK_FLOW] trace={trace_id} phone={phone} propiedad encontrada codigo={codigo_detectado} origen={nuevo_origen}")

    # Actualizar prospecto si encontramos propiedad nueva
    if propiedad and codigo_detectado:
        prop_loc = get_prop_location(propiedad)
        prop_op = get_prop_operation(propiedad)
        updates_prop = {
            "ultimo_mensaje": datetime.now(CHILE_TZ).isoformat(),
            "codigo": codigo_detectado,
            "precio_uf": prop_op.get("precio_uf"),
            "comuna": prop_loc.get("comuna"),
            "tipo": prop_op.get("tipo"),
            "operacion": prop_op.get("operacion"),
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
        asyncio.create_task(send_alert_once(
            phone=admin_phone,
            lead_type="MissingProperty",
            lead_score=0,
            criteria={
                "nombre": "Admin Pablo",
                "codigo_faltante": codigo_externo,
                "link_pendiente": True,
                "codigo": "",
            },
            last_response=admin_msg,
            last_user_msg=original_message,
            full_history=[],
            window_minutes=120,
            lead_type_label="PROPIEDAD FALTANTE"
        ))
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
    datos_necesarios = {
        "Nombre": prospecto_actual.get("nombre"),
        "RUT": prospecto_actual.get("rut"),
        "Email": prospecto_actual.get("email")
    }
    faltantes = [k for k, v in datos_necesarios.items() if not v]
    
    if faltantes:
        system_parts.append(f"""
        [ESTADO DE DATOS DEL CLIENTE]
        Datos que FALTAN para Orden de Visita: {', '.join(faltantes)}.
        INSTRUCCIÓN: Si hay intención clara de visitar, solicítalos amablemente.
        """)
    else:
        system_parts.append("[ESTADO] ¡Tenemos todos los datos (Nombre, RUT, Email)! Solo coordina preferencia de hora (No confirmes, solo registra).")

    # --- CONTEXTO 2: INFORMACIÓN DE PROPIEDADES (PRIORIDAD: ESPECÍFICA > BÚSQUEDA) ---
    
    # CASO A: Propiedad Específica (Link o Código)
    if propiedad:
        ficha_texto = formatear_ficha_tecnica(propiedad)
        system_parts.append(f"""
        [DATOS OFICIALES DE LA PROPIEDAD ACTIVA]
        {ficha_texto}
        """)

    # CASO B: Búsqueda / RAG (Solo si no hay propiedad específica activa)
    else:
        # Definir criterios de búsqueda basados en el prospecto
        criterios_rag = {
            "operacion": prospecto_actual.get("operacion"),
            "tipo": prospecto_actual.get("tipo"),
            "comuna": prospecto_actual.get("comuna"),
            "dormitorios": prospecto_actual.get("dormitorios"),
            "presupuesto": prospecto_actual.get("presupuesto")
        }

        # LÓGICA RAG: Solo ejecutamos RAG si el cliente está buscando activamente O la conversación es nueva
        is_search_intent = any(x in msg_lower for x in ["busco", "otra", "tienes", "opciones", "más"])
        is_initial_search = len(historial) <= 6 # Heurística para etapas tempranas
        
        if criterios_rag["operacion"] and criterios_rag["comuna"] and (is_search_intent or is_initial_search):
            
            # --- LÓGICA CLAVE: ANTI-REPETICIÓN Y LÍMITE ---
            codigos_vistos = obtener_propiedades_vistas(phone)
            
            # Buscamos excluyendo lo visto y limitando a 3 (o el límite que se defina)
            # USAMOS BÚSQUEDA SEMÁNTICA (HÍBRIDA) EN VEZ DE LA SIMPLE
            resultados_rag = buscar_semanticamente(
                original_message, 
                exclude_codes=codigos_vistos, 
                limit=3
            )
            
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
            else:
                system_parts.append(f"""
                [SISTEMA DE BÚSQUEDA]
                Buscamos con: {json.dumps(criterios_rag, ensure_ascii=False)} y NO hay más resultados nuevos con esos filtros.
                INSTRUCCIÓN: Informa que no hay más opciones con esos filtros exactos, pregunta si quiere ampliar la búsqueda.
                """)
        
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

    # Insertar Contexto Dinámico al final del System Prompt
    if system_parts:
        full_context = "\n\n".join(system_parts)
        messages_para_grok.append({"role": "system", "content": full_context})

    # Mensaje final del usuario
    messages_para_grok.append({"role": "user", "content": original_message})

    # =======================================================
    # 6. RESPUESTA CON GROK (Generación + Extracción)
    # =======================================================
    # CORRECCIÓN: Recargar prospecto_actual para reflejar el codigo ya guardado (16479)
    # y para que el log [DEEPSEEK PROPERTY_PAYLOAD] sea exacto y no muestre el código anterior.
    prospecto_actual = await _run_sync(obtener_prospecto, phone) or {}
    logger.info(
        f"[PROPERTY_TRACE] origen=PRE_DEEPSEEK trace={trace_id} phone={phone} "
        f"prospecto.codigo={prospecto_actual.get('codigo')} prospecto.comuna={prospecto_actual.get('comuna')}"
    )

    # Campos que el modelo IA tiene PROHIBIDO sobrescribir (datos resueltos por el sistema)
    _CAMPOS_BLOQUEADOS_IA = {"codigo", "comuna", "tipo", "operacion", "precio_uf", "link_detectado", "link_pendiente", "origen", "codigo_yapo", "codigo_mercadolibre"}

    try:
        resultado_grok = await _run_sync(generar_respuesta_estructurada, messages_para_grok, prospecto_actual)

        intencion = resultado_grok["intencion"]
        respuesta = resultado_grok["respuesta_bot"]
        datos_extraidos = resultado_grok.get("datos_extraidos", {})

        # Guardar nuevos datos detectados por IA — SOLO campos permitidos
        if datos_extraidos:
            datos_seguros = {k: v for k, v in datos_extraidos.items() if k not in _CAMPOS_BLOQUEADOS_IA}
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

    except Exception as e:
        logger.error(f"Error Grok: {e}")
        intencion = "consulta_general"
        respuesta = "Disculpa, tengo un problema técnico momentáneo."

    # =======================================================
    # 7. EXCEPCIÓN: FORZAR FICHA (RESPALDO ORIGINAL)
    # =======================================================
    if propiedad and "ficha" in original_message.lower():
         ficha_completa = formatear_ficha_tecnica(propiedad)
         respuesta = f"Aquí tienes el resumen técnico completo:\n\n{ficha_completa}"

    # =======================================================
    # 8. POST-PROCESO DE EMAIL (RESPALDO ORIGINAL)
    # =======================================================
    # Esto es redundante con la extracción proactiva, pero lo dejamos como seguro
    if not prospecto_actual.get("email"):
        email_detectado = extraer_email(original_message)
        if email_detectado:
             await _run_sync(actualizar_prospecto, phone, {"email": email_detectado.lower()})

    # --- GUARDRAIL DE INTENCIÓN (REGLAS DETERMINÍSTICAS) ---
    # Evita falsos "consulta_general" cuando el usuario expresa intención explícita.
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
        if any(t in msg_l for t in visit_terms):
            intencion = "agendar_visita"
        elif any(t in msg_l for t in contact_terms):
            intencion = "contacto_directo"

    # --- NUEVA LÓGICA DE INTENCIÓN (ENTERPRISE) ---
    intent_map = {
        "agendar_visita": LeadIntent.ASK_VISIT,
        "contacto_directo": LeadIntent.ASK_INFO,
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

    # CORRECCIÓN DE TIEMPOS: 60 minutos para evitar spam de correo
    if intencion == "escalado_urgente":
        await _run_sync(record_observability_event, "ALERT_TRIGGERED", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
            "phone": phone,
            "alert_type": "EscaladoUrgente"
        })
        asyncio.create_task(send_alert_once(phone=phone, lead_type="EscaladoUrgente", lead_score=lead_score,
                        criteria=prospecto_actual, last_response=respuesta, last_user_msg=original_message,
                        full_history=historial, window_minutes=60, lead_type_label="ESCALADO URGENTE"))
        metadata_tipo = {"tipo": "escalado_urgente", "intencion": intencion}

    elif intencion == "agendar_visita":
        await _run_sync(record_observability_event, "ALERT_TRIGGERED", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
            "phone": phone,
            "alert_type": "InteresVisita"
        })
        asyncio.create_task(send_alert_once(phone=phone, lead_type="InteresVisita", lead_score=lead_score,
                        criteria=prospecto_actual, last_response=respuesta, last_user_msg=original_message,
                        full_history=historial, window_minutes=60, lead_type_label="Interés de Visita"))
        metadata_tipo = {"tipo": "gestion_visita", "intencion": intencion}

    elif intencion == "contacto_directo":
        await _run_sync(record_observability_event, "ALERT_TRIGGERED", {
            "conversation_id": conversation_id,
            "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
            "phone": phone,
            "alert_type": "SolicitudContacto"
        })
        asyncio.create_task(send_alert_once(phone=phone, lead_type="SolicitudContacto", lead_score=lead_score,
                        criteria=prospecto_actual, last_response=respuesta, last_user_msg=original_message,
                        full_history=historial, window_minutes=60, lead_type_label="Solicitud de Contacto"))
        metadata_tipo = {"tipo": "contacto_directo", "intencion": intencion}

    # =======================================================
    # 10. GUARDAR Y RETORNAR (COMPLETO)
    # =======================================================
    try:
        # Log del evento estructurado (Ya usa InteractionType.BOT_MSG)
        await _run_sync(log_event, phone, InteractionType.BOT_MSG, "bot", {
            "text": respuesta, 
            "intencion": intencion,
            "lead_intent": selected_intent
        })
        
        # No auto-promovemos a CONTACTED. El lead se queda en NEW (Rojo) hasta que el humano gestante lo tome.
        pass
            
    except Exception as ex_log:
        logger.error(f"Error logging bot event: {ex_log}")

    logger.info(
        "[MONGO_SAVE_SIZE] respuesta_len=%s",
        len(respuesta or "")
    )
    await _run_sync(guardar_mensaje, phone, "assistant", respuesta, metadata_tipo)
    await _run_sync(record_observability_event, "RESPONSE_SENT", {
        "conversation_id": conversation_id,
        "lead_id": str(lead_doc.get("_id")) if lead_doc.get("_id") else None,
        "phone": phone,
        "intent": intencion,
        "response_len": len(respuesta or "")
    })
    return respuesta
