# chatbot/core.py
import logging
import re
import json
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
    obtener_propiedades_vistas    # NUEVA IMPORTACIÓN (Anti-repetición)
)
from .grok_client import generar_respuesta, generar_respuesta_estructurada
from .link_extractor import analizar_mensaje_para_link
from .utils import extraer_rut, extraer_email, safe_int_conversion, extraer_nombre_explicito
from .alert_service import send_alert_once
from .classifier import es_propietario 

# RAG IMPORT
from .rag import buscar_propiedades, formatear_resultados_texto
# Importamos el prompt maestro con las reglas estrictas (No horarios, no inventar)
from .prompts import SYSTEM_PROMPT_PROSPECTO 

logger = logging.getLogger(__name__)

# ==========================================
#   LEAD SCORE
# ==========================================
def calcular_lead_score(intencion: str, prospecto: dict) -> int:
    score = 0
    if intencion == "agendar_visita": score += 6
    if intencion == "contacto_directo": score += 7
    if intencion == "escalado_urgente": score += 10
    if prospecto.get("email"): score += 2
    if prospecto.get("nombre"): score += 2
    if prospecto.get("rut"): score += 3
    return score

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

def process_user_message(phone: str, message: str) -> str:
    original_message = message
    msg_lower = original_message.lower()
    
    # 1. Guardar mensaje usuario
    guardar_mensaje(phone, "user", original_message)

# === MODIFICACIÓN PARA FORZAR EL ORIGEN A WHATSAPP ===
    actualizar_prospecto(phone, {"origen": "WhatsApp"})

    historial = obtener_conversacion(phone)

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
    # 4. ANÁLISIS DE PROPIEDAD (LINK O CÓDIGO)
    # =======================================================
    propiedad = None
    nuevo_origen = None
    codigo_mercadolibre = None
    codigo_detectado = None
    
    # 1. Intentar detectar Link o Código en el mensaje actual
    es_link, temp_prop, plataforma_origen, codigo_ml_externo = analizar_mensaje_para_link(original_message)

    if es_link and temp_prop:
        propiedad = temp_prop
        nuevo_origen = plataforma_origen
        codigo_detectado = str(propiedad.get("codigo"))
        codigo_mercadolibre = codigo_ml_externo
    elif es_link and not temp_prop:
        nuevo_origen = plataforma_origen
        codigo_mercadolibre = codigo_ml_externo

    if not propiedad:
        # Buscar código numérico explícito en el mensaje
        match = re.search(r"\b(\d{4,6})\b", original_message)
        if match:
            cod = match.group(1)
            propiedad = get_db()[Config.COLLECTION_NAME].find_one({"$or": [{"codigo": cod}, {"codigo": safe_int_conversion(cod)}]})
            if propiedad:
                codigo_detectado = str(propiedad.get("codigo"))
                if not prospecto_actual.get("origen"):
                    nuevo_origen = "WhatsApp"

    # 2. Si NO hay propiedad en mensaje actual, recuperar histórica SOLO SI NO ESTAMOS BUSCANDO OTRA COSA
    if not propiedad and not any(x in msg_lower for x in ["busco", "otra", "tienes", "opciones"]):
        codigo_guardado = prospecto_actual.get("codigo")
        if codigo_guardado:
            propiedad = get_db()[Config.COLLECTION_NAME].find_one({"$or": [{"codigo": codigo_guardado}, {"codigo": safe_int_conversion(codigo_guardado)}]})

    # Actualizar prospecto si encontramos propiedad nueva
    if propiedad and codigo_detectado:
        updates_prop = {
            "ultimo_mensaje": datetime.utcnow().isoformat(),
            "codigo": codigo_detectado,
            "precio_uf": propiedad.get("precio_uf"),
            "comuna": propiedad.get("comuna"),
            "tipo": propiedad.get("tipo"),
            "operacion": propiedad.get("operacion")
        }
        if nuevo_origen: updates_prop["origen"] = nuevo_origen
        if codigo_mercadolibre: updates_prop["codigo_mercadolibre"] = codigo_mercadolibre
        actualizar_prospecto(phone, updates_prop)


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
            resultados_rag = buscar_propiedades(
                criterios_rag, 
                exclude_codes=codigos_vistos, 
                limit=3
            )
            
            texto_rag = formatear_resultados_texto(resultados_rag)
            
            if resultados_rag:
                # Registramos las nuevas como vistas (solo si las vamos a mostrar)
                nuevos_codigos = [p["codigo"] for p in resultados_rag]
                registrar_propiedades_vistas(phone, nuevos_codigos)
                
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

    # =======================================================
    # 9. ENVÍO DE ALERTAS Y METADATA (LÓGICA MEJORADA ANTI-DUPLICADOS)
    # =======================================================
    metadata_tipo = {"tipo": "respuesta_general", "intencion": intencion}
    prospecto_actual = obtener_prospecto(phone) or {} # Recargamos prospecto para lead score
    lead_score = calcular_lead_score(intencion, prospecto_actual)

    # CORRECCIÓN DE TIEMPOS: 60 minutos para evitar spam de correo
    if intencion == "escalado_urgente":
        send_alert_once(phone=phone, lead_type="EscaladoUrgente", lead_score=lead_score,
                        criteria=prospecto_actual, last_response=respuesta, last_user_msg=original_message,
                        full_history=historial, window_minutes=1, lead_type_label="ESCALADO URGENTE")
        metadata_tipo = {"tipo": "escalado_urgente", "intencion": intencion}

    elif intencion == "agendar_visita":
        send_alert_once(phone=phone, lead_type="InteresVisita", lead_score=lead_score,
                        criteria=prospecto_actual, last_response=respuesta, last_user_msg=original_message,
                        full_history=historial, window_minutes=1, lead_type_label="Interés de Visita") # AJUSTADO A 60 MIN
        metadata_tipo = {"tipo": "gestion_visita", "intencion": intencion}

    elif intencion == "contacto_directo":
        send_alert_once(phone=phone, lead_type="SolicitudContacto", lead_score=lead_score,
                        criteria=prospecto_actual, last_response=respuesta, last_user_msg=original_message,
                        full_history=historial, window_minutes=1, lead_type_label="Solicitud de Contacto") # AJUSTADO A 60 MIN
        metadata_tipo = {"tipo": "contacto_directo", "intencion": intencion}

    # =======================================================
    # 10. GUARDAR Y RETORNAR (COMPLETO)
    # =======================================================
    guardar_mensaje(phone, "assistant", respuesta, metadata_tipo)
    return respuesta