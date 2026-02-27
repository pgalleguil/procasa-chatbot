import logging
import re
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
    log_event
)
from .crm_service import CrmService
from .constants import PipelineStage, InteractionType, LeadIntent, CHILE_TZ

from .grok_client import generar_respuesta, generar_respuesta_estructurada
from .link_extractor import analizar_mensaje_para_link
from .utils import extraer_rut, extraer_email, safe_int_conversion, extraer_nombre_explicito
from .alert_service import send_alert_once
from .classifier import es_propietario 

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
    """
    precio_clp = safe_int_conversion(propiedad.get('precio_clp', 0))
    gastos_comunes = safe_int_conversion(propiedad.get('gastos_comunes', 0))
    
    return f"""
    Código: {propiedad.get('codigo', 'N/D')}
    Tipo: {propiedad.get('tipo', 'Departamento').title()}
    Operación: {propiedad.get('operacion', 'Venta').title()}
    Comuna: {propiedad.get('comuna', 'Santiago').title()}
    Precio: {propiedad.get('precio_uf', 'N/D')} UF | ${precio_clp:,}
    Metros útiles: {propiedad.get('m2_utiles', 'N/D')} m²
    Metros totales: {propiedad.get('m2_totales', 'N/D')} m²
    Terraza: {propiedad.get('m2_terraza', '0')} m²
    Dormitorios: {propiedad.get('dormitorios', 'N/D')}
    Baños: {propiedad.get('banos', 'N/D')}
    Estacionamientos: {propiedad.get('estacionamientos', '0')}
    Bodega: {'Sí' if str(propiedad.get('bodega','')).lower() in ['sí','si','1'] else 'No'}
    Gastos comunes: ${gastos_comunes:,}
    Orientación: {propiedad.get('orientacion', 'No especificada')}
    Calefacción: {propiedad.get('calefaccion', 'No especificada')}
    Piscina: {propiedad.get('piscina', 'No')}
    Quincho: {'Sí' if str(propiedad.get('quincho','')).lower() in ['sí','si','1'] else 'No'}
    Gimnasio: {'Sí' if str(propiedad.get('gimnasio','')).lower() in ['sí','si','1'] else 'No'}
    Ubicación Referencial: {propiedad.get('nombre_calle', '')}
    Amenities: {propiedad.get('amenities_text', '')[:200]}...
    Descripción: {propiedad.get('descripcion_clean', '')[:300]}...
    """

# ==========================================
#   PROCESADOR PRINCIPAL
# ==========================================


async def process_user_message(phone: str, message: str, is_from_me: bool = False) -> str:
    original_message = message
    msg_lower = original_message.lower()
    
    # 1. Guardar mensaje (con el rol correcto)
    # Si viene de 'me' (del dueño del bot), lo guardamos como assistant/human
    role = "assistant" if is_from_me else "user"
    guardar_mensaje(phone, role, original_message)

    # === LÓGICA DE PAUSA (INTERCEPCIÓN) ===
    from .storage import obtener_bot_pausado, toggle_bot_pausado
    
    if original_message.strip() == "..":
        nuevo_estado = toggle_bot_pausado(phone)
        if nuevo_estado:
            logger.info(f"🤖 [TOGGLE] Bot PAUSADO para {phone}")
        else:
            logger.info(f"🤖 [TOGGLE] Bot REACTIVADO para {phone}")
        
        # Guardamos en DB para historial interno pero retornamos vacío para NO enviar a WhatsApp
        guardar_mensaje(phone, "assistant", f"Bot {'Pausado' if nuevo_estado else 'Reactivado'} (Comando ..)", {"tipo": "bot_toggle"})
        return "" 

    # Si es un mensaje manual del agente (is_from_me), no hacemos nada más, 
    # solo lo dejamos guardado en el historial arriba.
    if is_from_me:
        logger.info(f"[MANUAL] Mensaje manual detectado para {phone}. Guardado en contexto.")
        return ""

    # Si el bot está pausado, NO procesamos ni respondemos para el cliente
    if obtener_bot_pausado(phone):
        logger.info(f"[PAUSED] Bot pausado para {phone}. Ignorando procesamiento.")
        return "" 

    historial = obtener_conversacion(phone)

    # === OBTENEMOS PROSPECTO TEMPRANO PARA PODER USARLO EN ORIGEN Y EN TODO EL FLUJO ===
    prospecto_actual = obtener_prospecto(phone) or {}

    # Solo forzar WhatsApp como fallback si no hay origen previo (permite Yapo, MercadoLibre, etc.)
    if not prospecto_actual.get("origen"):
        actualizar_prospecto(phone, {"origen": "WhatsApp"})

    # =======================================================
    # 2. FLUJO PROPIETARIO
    # =======================================================
    es_prop, nombre_prop = es_propietario(phone) 
    if es_prop:
        prompt_propietario = f"Eres asistente Procasa para propietarios. Habla directo y claro con {nombre_prop}. Responde cualquier consulta sobre su propiedad o venta."
        respuesta = generar_respuesta(
            [{"role": "system", "content": prompt_propietario}, *historial[-20:], {"role": "user", "content": original_message}],
            "propietario"
        )
        guardar_mensaje(phone, "assistant", respuesta, {"tipo": "propietario_atencion"})
        return respuesta

    # =======================================================
    # 3. ANÁLISIS PRELIMINAR DE DATOS Y EXTRACCIÓN PROACTIVA
    # =======================================================
    prospecto_actual = obtener_prospecto(phone) or {} 
    updates_datos = {}
    
    # A) EXTRACCIÓN PROACTIVA DE DATOS PERSONALES
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
        actualizar_prospecto(phone, updates_datos)
        prospecto_actual.update(updates_datos)

    # =======================================================
    # 4. ANÁLISIS DE PROPIEDAD (LINK O CÓDIGO) - VERSIÓN CORREGIDA
    # =======================================================
    propiedad = None
    nuevo_origen = None
    codigo_externo = None  # Genérico: será yapo o mercadolibre según plataforma
    codigo_detectado = None
    
    # 1. Intentar detectar Link o Código en el mensaje actual
    es_link, temp_prop, plataforma_origen, codigo_externo_raw = analizar_mensaje_para_link(original_message)

    if es_link and temp_prop:
        propiedad = temp_prop
        nuevo_origen = plataforma_origen or "WhatsApp"
        codigo_detectado = str(propiedad.get("codigo"))
        codigo_externo = codigo_externo_raw  # Guardamos el código externo crudo
    elif es_link and not temp_prop:
        nuevo_origen = plataforma_origen or "WhatsApp"
        codigo_externo = codigo_externo_raw

    if not propiedad:
        # Buscar código numérico explícito en el mensaje (código Procasa interno)
        match = re.search(r"\b(\d{4,6})\b", original_message)
        if match:
            cod = match.group(1)
            propiedad = get_db()[Config.COLLECTION_NAME].find_one({"$or": [{"codigo": cod}, {"codigo": safe_int_conversion(cod)}]})
            if propiedad:
                codigo_detectado = str(propiedad.get("codigo"))
                if not prospecto_actual.get("origen"):
                    nuevo_origen = "WhatsApp"

    # 2. Si NO hay propiedad en mensaje actual, recuperar histórica
    if not propiedad and not any(x in msg_lower for x in ["busco", "otra", "tienes", "opciones"]):
        codigo_guardado = prospecto_actual.get("codigo")
        if codigo_guardado:
            propiedad = get_db()[Config.COLLECTION_NAME].find_one({"$or": [{"codigo": codigo_guardado}, {"codigo": safe_int_conversion(codigo_guardado)}]})

    # Actualizar prospecto si encontramos propiedad nueva
    if propiedad and codigo_detectado:
        updates_prop = {
            "ultimo_mensaje": datetime.now(CHILE_TZ).isoformat(),
            "codigo": codigo_detectado,
            "precio_uf": propiedad.get("precio_uf"),
            "comuna": propiedad.get("comuna"),
            "tipo": propiedad.get("tipo"),
            "operacion": propiedad.get("operacion"),
            "origen": nuevo_origen  # Siempre actualiza origen si viene de link
        }
        actualizar_prospecto(phone, updates_prop)
        
        # Registrar para anti-repetición en RAG
        registrar_propiedades_vistas(phone, [codigo_detectado])

    # === CORRECCIÓN: Guardar código externo aunque no esté en DB ===
    if codigo_externo:
        ext_updates = {"origen": nuevo_origen}
        if plataforma_origen == "Yapo":
            ext_updates["codigo_yapo"] = codigo_externo
        elif plataforma_origen in ["MercadoLibre", "PortalInmobiliario", "Otro Portal (MLC code)"]:
            ext_updates["codigo_mercadolibre"] = codigo_externo
        
        # SOLO guardamos el código externo si NO encontramos una propiedad de Procasa
        # o si queremos mantener la trazabilidad del link original.
        # Pero nos aseguramos de no tocar el campo 'codigo' si ya tiene un valor de Procasa.
        actualizar_prospecto(phone, ext_updates)

    # --- NOTIFICACIÓN POR PROPIEDAD DESCONOCIDA ---
    if es_link and not propiedad and codigo_externo:
        # Si es un link pero no encontramos propiedad, notificamos al Admin (Pablo Galleguillos)
        from .lead_router import find_responsible_executive
        # Re-verificamos responsable para gatillar alerta si sigue desasignado
        exec_name, _ = find_responsible_executive(codigo_externo)
        
        if exec_name == "No Asignado":
            admin_phone = "56983219804" # Pablo Galleguillos
            admin_msg = (
                f"🚨 *Propiedad No Encontrada*\n\n"
                f"El cliente {prospecto_actual.get('nombre', 'Desconocido')} ({phone}) "
                f"envió un link de {plataforma_origen or 'Portal'} con código `{codigo_externo}`, "
                f"pero no existe en `universo_obelix`.\n\n"
                f"El lead quedó *No Asignado*. Favor actualizar códigos o ingresar propiedad."
            )
            # Usamos send_alert_once para notificar al admin
            asyncio.create_task(send_alert_once(
                phone=admin_phone, 
                lead_type="MissingProperty", 
                lead_score=0,
                criteria={"nombre": "Admin Pablo", "codigo_faltante": codigo_externo},
                last_response=admin_msg,
                last_user_msg=original_message,
                full_history=[],
                window_minutes=120, # No spamear al admin si el mismo link llega varias veces
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
                    registrar_propiedades_vistas(phone, nuevos_codigos)
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
    try:
        resultado_grok = generar_respuesta_estructurada(messages_para_grok, prospecto_actual)
        
        intencion = resultado_grok["intencion"]
        respuesta = resultado_grok["respuesta_bot"]
        datos_extraidos = resultado_grok.get("datos_extraidos", {})
        
        # Guardar nuevos datos detectados por IA
        if datos_extraidos:
            actualizar_prospecto(phone, datos_extraidos)

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
             actualizar_prospecto(phone, {"email": email_detectado.lower()})

    # --- NUEVA LÓGICA DE INTENCIÓN (ENTERPRISE) ---
    intent_map = {
        "agendar_visita": LeadIntent.ASK_VISIT,
        "contacto_directo": LeadIntent.ASK_INFO,
        "escalado_urgente": LeadIntent.ASK_INFO, # Fallback a info + alerta
        "consulta_general": LeadIntent.ASK_INFO
    }
    
    selected_intent = intent_map.get(intencion, LeadIntent.OTHER)
    CrmService.update_intent(phone, selected_intent, actor="bot")

    # =======================================================
    # 9. ENVÍO DE ALERTAS...
    # =======================================================
    metadata_tipo = {"tipo": "respuesta_general", "intencion": intencion, "lead_intent": selected_intent}
    prospecto_actual = obtener_prospecto(phone) or {} # Recargamos prospecto para lead score
    lead_doc = CrmService.get_lead(phone) or {} # Obtenemos documento completo para score
    lead_score = CrmService.calculate_score(lead_doc)

    # CORRECCIÓN DE TIEMPOS: 60 minutos para evitar spam de correo
    if intencion == "escalado_urgente":
        asyncio.create_task(send_alert_once(phone=phone, lead_type="EscaladoUrgente", lead_score=lead_score,
                        criteria=prospecto_actual, last_response=respuesta, last_user_msg=original_message,
                        full_history=historial, window_minutes=60, lead_type_label="ESCALADO URGENTE"))
        metadata_tipo = {"tipo": "escalado_urgente", "intencion": intencion}

    elif intencion == "agendar_visita":
        asyncio.create_task(send_alert_once(phone=phone, lead_type="InteresVisita", lead_score=lead_score,
                        criteria=prospecto_actual, last_response=respuesta, last_user_msg=original_message,
                        full_history=historial, window_minutes=60, lead_type_label="Interés de Visita"))
        metadata_tipo = {"tipo": "gestion_visita", "intencion": intencion}

    elif intencion == "contacto_directo":
        asyncio.create_task(send_alert_once(phone=phone, lead_type="SolicitudContacto", lead_score=lead_score,
                        criteria=prospecto_actual, last_response=respuesta, last_user_msg=original_message,
                        full_history=historial, window_minutes=60, lead_type_label="Solicitud de Contacto"))
        metadata_tipo = {"tipo": "contacto_directo", "intencion": intencion}

    # =======================================================
    # 10. GUARDAR Y RETORNAR (COMPLETO)
    # =======================================================
    try:
        # Log del evento estructurado (Ya usa InteractionType.BOT_MSG)
        log_event(phone, InteractionType.BOT_MSG, "bot", {
            "text": respuesta, 
            "intencion": intencion,
            "lead_intent": selected_intent
        })
        
        # No auto-promovemos a CONTACTED. El lead se queda en NEW (Rojo) hasta que el humano gestante lo tome.
        pass
            
    except Exception as ex_log:
        logger.error(f"Error logging bot event: {ex_log}")

    guardar_mensaje(phone, "assistant", respuesta, metadata_tipo)
    return respuesta