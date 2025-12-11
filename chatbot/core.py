# chatbot/core.py
import logging
import re
from datetime import datetime

from config import Config
from .storage import (
    guardar_mensaje, 
    obtener_conversacion, 
    get_db, 
    actualizar_prospecto, 
    obtener_prospecto,
    establecer_nombre_usuario
)
from .grok_client import generar_respuesta
from .link_extractor import analizar_mensaje_para_link
from .utils import extraer_rut, extraer_email, extraer_nombre_posible

# IMPORTS ACTUALIZADOS
from .alert_service import send_alert_once
# Ahora usamos estas funciones del módulo classifier
from .classifier import detectar_intencion_con_ai, es_propietario 

logger = logging.getLogger(__name__)

# ==========================================
#   LEAD SCORE
# ==========================================

def calcular_lead_score(intencion: str, prospecto: dict) -> int:
    score = 0

    if intencion == "agendar_visita":
        score += 6
    if intencion == "contacto_directo":
        score += 7
    if intencion == "escalado_urgente":
        score += 10

    if prospecto.get("email"): score += 2
    if prospecto.get("nombre"): score += 1
    if prospecto.get("rut"): score += 1

    return min(score, 10)


# ==========================================
#   FICHA SEGURA (COMPLETA CON TODOS LOS CAMPOS)
# ==========================================

def formatear_ficha_tecnica(propiedad):
    return f"""
    Código: {propiedad.get('codigo', 'N/D')}
    Tipo: {propiedad.get('tipo', 'Departamento').title()}
    Operación: {propiedad.get('operacion', 'Venta').title()}
    Comuna: {propiedad.get('comuna', 'Santiago').title()}
    Precio: {propiedad.get('precio_uf', 'N/D')} UF | ${int(propiedad.get('precio_clp', 0)):,}
    Metros útiles: {propiedad.get('m2_utiles', 'N/D')} m²
    Metros totales: {propiedad.get('m2_totales', 'N/D')} m²
    Terraza: {propiedad.get('m2_terraza', '0')} m²
    Dormitorios: {propiedad.get('dormitorios', 'N/D')}
    Baños: {propiedad.get('banos', 'N/D')}
    Estacionamientos: {propiedad.get('estacionamientos', '0')}
    Bodega: {'Sí' if str(propiedad.get('bodega','')).lower() in ['sí','si','1'] else 'No'}
    Gastos comunes: ${int(propiedad.get('gastos_comunes', 0)):,}
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
    
    # 1. Guardar mensaje usuario
    guardar_mensaje(phone, "user", original_message)
    historial = obtener_conversacion(phone) # Obtenemos historial fresco

    # =======================================================
    # 2. FLUJO PROPIETARIO (Revisión antes de todo)
    # =======================================================
    es_prop, nombre_prop = es_propietario(phone) 
    if es_prop:
        prompt_propietario = f"Eres asistente Procasa para propietarios. Habla directo y claro con {nombre_prop}. Responde cualquier consulta sobre su propiedad o venta, usando un tono profesional y cortés."
        respuesta = generar_respuesta(
            [
                {"role": "system", "content": prompt_propietario},
                *historial[-20:],
                {"role": "user", "content": original_message}
            ],
            "propietario"
        )
        guardar_mensaje(phone, "assistant", respuesta, {"tipo": "propietario_atencion"})
        return respuesta


    # =======================================================
    # 3. ANÁLISIS PRELIMINAR DE DATOS (Links/Códigos)
    # =======================================================
    prospecto_actual = obtener_prospecto(phone)
    propiedad = None
    es_link, temp_prop, origen_str = analizar_mensaje_para_link(original_message)
    nuevo_origen = None
    codigo_detectado = None

    # 3A — LINK CON PROPIEDAD
    if es_link and temp_prop:
        propiedad = temp_prop
        nuevo_origen = origen_str
        codigo_detectado = str(propiedad.get("codigo"))

    # 3B — CÓDIGO MANUAL
    if not propiedad:
        match = re.search(r"\b(\d{4,6})\b", original_message)
        if match:
            cod = match.group(1)
            propiedad = get_db()[Config.COLLECTION_NAME].find_one(
                {"$or": [{"codigo": cod}, {"codigo": int(cod)}]}
            )
            if propiedad:
                codigo_detectado = str(propiedad.get("codigo"))
                if not prospecto_actual.get("origen"):
                    nuevo_origen = "Chat/Codigo_Directo"

    # =======================================================
    # 4. ACTUALIZAR PROSPECTO (CRITERIA COMPLETA PARA EMAIL)
    # =======================================================
    updates = {
        "ultimo_mensaje": datetime.utcnow().isoformat()
    }

    # Si encontramos propiedad, guardamos TODA la info técnica en el prospecto
    if codigo_detectado:
        updates["codigo"] = codigo_detectado
        if propiedad:
            # Asegurando que todos los campos solicitados estén aquí
            updates["precio_uf"] = propiedad.get("precio_uf")
            updates["precio_clp"] = propiedad.get("precio_clp")
            updates["comuna"] = propiedad.get("comuna")
            updates["tipo"] = propiedad.get("tipo")
            updates["operacion"] = propiedad.get("operacion")
            updates["dormitorios"] = propiedad.get("dormitorios")
            updates["banos"] = propiedad.get("banos")

    if nuevo_origen: updates["origen"] = nuevo_origen

    # Datos opcionales aportados por el usuario
    rut = extraer_rut(original_message)
    email = extraer_email(original_message)
    nombre = extraer_nombre_posible(original_message)

    if rut: updates["rut"] = rut
    if email: updates["email"] = email
    if nombre:
        updates["nombre"] = nombre
        establecer_nombre_usuario(phone, nombre)

    actualizar_prospecto(phone, updates)
    prospecto_actual = obtener_prospecto(phone) # RE-OBTENER para tener el objeto completo

    # 5. CLASIFICACIÓN DE INTENCIÓN CON IA
    intencion = detectar_intencion_con_ai(original_message, historial)
    actualizar_prospecto(phone, {"intencion_actual": intencion})
    
    # =======================================================
    # 6. LÓGICA DE RESPUESTA Y ALERTAS
    # =======================================================

    # -------------------------------------------------------
    # CASO A — ESCALADO URGENTE (Prioridad Máxima)
    # -------------------------------------------------------
    if intencion == "escalado_urgente":
        respuesta = (
            "Entiendo tu molestia. He notificado a un supervisor urgente. "
            "Un asesor inmobiliario te contactará a la brevedad."
        )

        send_alert_once(
            phone=phone,
            lead_type="EscaladoUrgente",
            lead_score=10,
            criteria=prospecto_actual,
            last_response=respuesta,
            last_user_msg=original_message,
            full_history=historial,
            window_minutes=5,
            lead_type_label="Escalado Urgente"
        )

        guardar_mensaje(phone, "assistant", respuesta, {"escalado_urgente": True})
        return respuesta


    # -------------------------------------------------------
    # CASO B — INTENCIÓN DE VISITA
    # -------------------------------------------------------
    if intencion == "agendar_visita":
        faltan_datos = not (prospecto_actual.get("nombre") and prospecto_actual.get("email"))
        
        system_prompt = f"""
        El cliente quiere visitar la propiedad.

        REGLAS:
        - No agendes hora exacta.
        - Dile que un asesor le contactará para coordinar.
        - Pide datos SOLO de forma opcional si faltan:
          "Si quieres agilizar la gestión, me puedes dejar tu nombre o email,
           pero no es obligatorio."
        - Tono amable y humano.
        """

        if not faltan_datos:
            respuesta = (
                f"¡Perfecto {prospecto_actual.get('nombre','')}! Ya tengo tus datos.\n"
                f"Un asesor inmobiliario te contactará en breve para coordinar la visita."
            )
            actualizar_prospecto(phone, {"estado": "listo_para_cierre"})
        else:
            respuesta = generar_respuesta(
                [
                    {"role": "system", "content": system_prompt},
                    *historial[-15:],
                    {"role": "user", "content": original_message}
                ],
                "prospecto"
            )

        # === ENVÍO ALERTA DE VISITA ===
        send_alert_once(
            phone=phone,
            lead_type="InteresVisita",
            lead_score=calcular_lead_score("agendar_visita", prospecto_actual),
            criteria=prospecto_actual,
            last_response=respuesta,
            last_user_msg=original_message,
            full_history=historial,
            window_minutes=5,
            lead_type_label="Interés de Visita"
        )

        guardar_mensaje(phone, "assistant", respuesta, {"tipo": "gestion_visita"})
        return respuesta


    # -------------------------------------------------------
    # CASO C — PRIMERA DETECCIÓN DE PROPIEDAD (Sin intención clara de visita)
    # -------------------------------------------------------
    if propiedad and not any("presentacion_propiedad" in m.get("metadata", {}).get("tipo", "") for m in historial[-5:]):
        ficha = formatear_ficha_tecnica(propiedad)

        prompt = f"""
        Eres ejecutiva de Procasa. Tono cercano pero profesional.
        Acabas de encontrar la ficha del código {propiedad.get('codigo')}.

        DATOS:
        {ficha}

        TU OBJETIVO:
        1. Confirma que la encontraste.
        2. Menciona precio + 2-3 características (Dorms, Baños, Metros).
        3. Pregunta: "¿Te gustaría coordinar una visita o tienes alguna duda?"
        """

        respuesta = generar_respuesta(
            [{"role": "system", "content": prompt}],
            "prospecto"
        )

        guardar_mensaje(phone, "assistant", respuesta, {"tipo": "presentacion_propiedad"})
        return respuesta


    # -------------------------------------------------------
    # CASO D — PREGUNTAS TÉCNICAS (Con código activo o en la conversación)
    # -------------------------------------------------------
    codigo_activo = prospecto_actual.get("codigo")

    if codigo_activo and intencion in ["consulta_precio", "consulta_ubicacion", "consulta_financiera"]:
        if not propiedad:
            propiedad = get_db()[Config.COLLECTION_NAME].find_one(
                {"codigo": {"$in": [codigo_activo, int(codigo_activo)]}}
            )

        ficha = formatear_ficha_tecnica(propiedad) if propiedad else "Ficha no disponible."

        system_prompt = f"""
        Eres asistente Procasa.
        El cliente pregunta por la propiedad {codigo_activo}.

        FICHA:
        {ficha}

        REGLAS:
        1. Si el dato está, respóndelo de forma concisa.
        2. Si NO está:
           "Ese dato no aparece en mi ficha, pero puedo pedírselo al ejecutivo."
        3. No inventes.
        4. Cierra amable: "¿Te interesa coordinar una visita?"
        """

        respuesta = generar_respuesta(
            [
                {"role": "system", "content": system_prompt},
                *historial[-15:],
                {"role": "user", "content": original_message}
            ],
            "prospecto"
        )

        # === ENVÍO ALERTA SOLO SI FALTA INFO ===
        if "no aparece en mi ficha" in respuesta.lower() or "pedírselo al ejecutivo" in respuesta.lower():
            send_alert_once(
                phone=phone,
                lead_type="DatoNoDisponible",
                lead_score=4,
                criteria=prospecto_actual,
                last_response=respuesta,
                last_user_msg=original_message,
                full_history=historial,
                window_minutes=5,
                lead_type_label="Dato No Disponible"
            )

        guardar_mensaje(phone, "assistant", respuesta)
        return respuesta

    # -------------------------------------------------------
    # CASO E — CLIENTE PIDE CONTACTO HUMANO
    # -------------------------------------------------------
    if intencion == "contacto_directo":
        
        respuesta = (
            "¡Perfecto! Un asesor inmobiliario te contactará en los próximos minutos.\n"
            "Si quieres acelerar la gestión, me puedes dejar tu nombre o email (opcional)."
        )

        lead_score = calcular_lead_score("contacto_directo", prospecto_actual)

        # === ENVÍO ALERTA CONTACTO ===
        send_alert_once(
            phone=phone,
            lead_type="SolicitudContacto",
            lead_score=lead_score,
            criteria=prospecto_actual,
            last_response=respuesta,
            last_user_msg=original_message,
            full_history=historial,
            window_minutes=5,
            lead_type_label="Solicitud de Contacto"
        )

        guardar_mensaje(phone, "assistant", respuesta, {"tipo": "contacto_directo"})
        return respuesta


    # -------------------------------------------------------
    # CASO F — CONSULTA GENERAL (DEFAULT)
    # -------------------------------------------------------
    respuesta = generar_respuesta(
        [
            {
                "role": "system",
                "content": """
                Eres asistente Procasa.
                Ayuda al cliente amablemente:
                - Pide el link o el código (5-6 dígitos)
                - Conversa natural, no robótica
                - Ofrécele orientación general o ayuda con su búsqueda.
                """
            },
            *historial[-15:],
            {"role": "user", "content": original_message}
        ],
        "prospecto"
    )

    guardar_mensaje(phone, "assistant", respuesta)
    return respuesta