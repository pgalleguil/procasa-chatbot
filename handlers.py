# handlers.py - Versión FINAL COMPLETA (250+ líneas reales)
import re
from datetime import datetime, timezone
from typing import List, Dict, Any

from constants import RESPONSES, RESPONSES_PROPIETARIO
from criteria_extractor import extract_criteria
from rag import buscar_propiedades, formatear_propiedad

# ===================================================================
# FUNCIÓN AUXILIAR: Desactivar contacto
# ===================================================================
def deactivate_contacto(telefono: str):
    from config import Config
    from pymongo import MongoClient
    config = Config()
    client = MongoClient(config.MONGO_URI)
    db = client[config.DB_NAME]
    db["contactos"].update_one(
        {"telefono": telefono},
        {"$set": {"activo": False, "fecha_desactivacion": datetime.now(timezone.utc)}}
    )

def handle_stop(phone, user_msg, tipo_contacto, contactos_collection, responses=None, deactivate_func=None):
    responses = responses or RESPONSES
    end_response = responses["stop"]
    metadata = {
        "intention": "stop",
        "response_detected": user_msg,
        "sentiment": "no_conectar",
        "action": "vetado - no contactar más"
    }
    contactos_collection.update_one(
        {"telefono": phone},
        {"$push": {"messages": [
            {"role": "user", "content": user_msg, "timestamp": datetime.now(timezone.utc), "metadata": metadata},
            {"role": "assistant", "content": end_response, "timestamp": datetime.now(timezone.utc), "metadata": metadata}
        ]}}
    )
    (deactivate_func or deactivate_contacto)(phone)
    print(f"[ALERT] Contacto {phone} ({tipo_contacto}) vetado.")
    return end_response


def handle_found(phone, user_msg, tipo_contacto, contactos_collection, responses=None):
    responses = responses or RESPONSES
    response = responses["found"]
    metadata = {"intention": "found", "action": "cerrado - ya encontró"}
    contactos_collection.update_one(
        {"telefono": phone},
        {"$push": {"messages": [
            {"role": "user", "content": user_msg, "timestamp": datetime.now(timezone.utc), "metadata": metadata},
            {"role": "assistant", "content": response, "timestamp": datetime.now(timezone.utc), "metadata": metadata}
        ]}}
    )
    print(f"[LOG] Lead {phone} ya encontró propiedad.")
    return response


def handle_waiting(phone, user_msg, tipo_contacto, contactos_collection, responses=None):
    responses = responses or RESPONSES
    response = responses["waiting"]
    metadata = {"intention": "waiting", "action": "pausa - seguimiento futuro"}
    contactos_collection.update_one(
        {"telefono": phone},
        {"$push": {"messages": [
            {"role": "user", "content": user_msg, "timestamp": datetime.now(timezone.utc), "metadata": metadata},
            {"role": "assistant", "content": response, "timestamp": datetime.now(timezone.utc), "metadata": metadata}
        ]}}
    )
    return response


def handle_advisor(phone, user_msg, history, tipo_contacto, contactos_collection, responses=None):
    responses = responses or RESPONSES
    response = responses["advisor"]
    metadata = {"intention": "advisor", "action": "escalado a humano"}

    # Guardar mensajes
    contactos_collection.update_one(
        {"telefono": phone},
        {"$push": {"messages": {
            "$each": [
                {"role": "user", "content": user_msg, "timestamp": datetime.now(timezone.utc), "metadata": metadata},
                {"role": "assistant", "content": response, "timestamp": datetime.now(timezone.utc), "metadata": metadata}
            ]
        }}}
    )

    print(f"[ESCALADO] Asesor solicitado para {phone}")

    # ===================================================================
    # ENVÍO AUTOMÁTICO DE EMAIL → ¡ESTO ES LO QUE FALTABA!
    # ===================================================================
    try:
        from email_utils import send_gmail_alert
        contacto = contactos_collection.find_one({"telefono": phone})
        criteria = (contacto or {}).get("criteria", {})
        full_history = (contacto or {}).get("messages", [])

        send_gmail_alert(
            phone=phone,
            lead_type="LEAD CALIENTE - PIDE ASESOR / VISITA",
            lead_score=9,  # Casi máximo
            criteria=criteria,
            last_user_msg=user_msg,
            last_response=response,
            full_history=full_history,
            chat_id=str(contacto.get("_id")) if contacto else None
        )
        print(f"[EMAIL ENVIADO] Alerta automática para {phone}")
    except Exception as e:
        print(f"[ERROR EMAIL] No se pudo enviar alerta para {phone}: {e}")

    return response

def handle_followup_advisor(phone, user_msg, history, tipo_contacto, contactos_collection, responses=None):
    responses = responses or RESPONSES
    response = responses["followup_advisor"]
    metadata = {"intention": "followup_advisor", "action": "re-escalado urgente"}

    contactos_collection.update_one(
        {"telefono": phone},
        {"$push": {"messages": {
            "$each": [
                {"role": "user", "content": user_msg, "timestamp": datetime.now(timezone.utc), "metadata": metadata},
                {"role": "assistant", "content": response, "timestamp": datetime.now(timezone.utc), "metadata": metadata}
            ]
        }}}
    )

    print(f"[RE-ESCALADO URGENTE] {phone}")

    # Email con urgencia máxima
    try:
        from email_utils import send_gmail_alert
        contacto = contactos_collection.find_one({"telefono": phone})
        criteria = (contacto or {}).get("criteria", {})
        full_history = (contacto or {}).get("messages", [])

        send_gmail_alert(
            phone=phone,
            lead_type="LEAD MUY CALIENTE - SE ESTÁ IMPACIENTANDO",
            lead_score=10,
            criteria=criteria,
            last_user_msg=user_msg,
            last_response=response,
            full_history=full_history,
            chat_id=str(contacto.get("_id")) if contacto else None
        )
        print(f"[EMAIL URGENTE ENVIADO] {phone}")
    except Exception as e:
        print(f"[ERROR EMAIL URGENTE] {e}")

    return response

def handle_closure(phone, user_msg, tipo_contacto, contactos_collection, responses=None):
    responses = responses or RESPONSES
    response = responses["closure"]
    metadata = {"intention": "closure", "action": "cerrado amistosamente"}
    contactos_collection.update_one(
        {"telefono": phone},
        {"$push": {"messages": [
            {"role": "user", "content": user_msg, "timestamp": datetime.now(timezone.utc), "metadata": metadata},
            {"role": "assistant", "content": response, "timestamp": datetime.now(timezone.utc), "metadata": metadata}
        ]}}
    )
    return response

"""
def handle_propietario_placeholder(phone, user_msg, history, contacto, contactos_collection, responses=None):
    responses = responses or RESPONSES
    codigo = contacto.get("codigo", "XXXXX")
    cliente = contacto.get("nombre_propietario", "Estimado/a")
    response = responses["propietario_placeholder"].format(cliente=cliente, codigo=codigo)
    metadata = {"intention": "propietario_response", "action": "placeholder propietario"}
    contactos_collection.update_one(
        {"telefono": phone},
        {"$push": {"messages": [
            {"role": "user", "content": user_msg, "timestamp": datetime.now(timezone.utc), "metadata": metadata},
            {"role": "assistant", "content": response, "timestamp": datetime.now(timezone.utc), "metadata": metadata}
        ]}}
    )
    return response
"""

def handle_propietario_respuesta(phone: str, user_msg: str, contacto: dict, contactos_collection) -> str:
    contacto = contacto or {}  # Blindaje total

    nombre_raw = contacto.get("nombre_propietario")
    nombre_completo = nombre_raw if isinstance(nombre_raw, str) and nombre_raw.strip() else "dueño/a"
    primer_nombre = nombre_completo.strip().split(maxsplit=1)[0].title()
    codigo = contacto.get("codigo", "sin código")

    texto = user_msg.strip().lower()
    original = user_msg.strip()

    accion = "desconocido"
    score = 5

    # ===================================================================
    # 1. AUTORIZACIÓN DE BAJA
    # ===================================================================
    REGEX_BAJA = re.compile(
        r'\b(1|uno|baj(?:ar|en|emos|arle|emosle|émoslo|ale|bájen(?:la|lo|me)?)|'
        r'ajust(?:ar|emos|e|émoslo|émosle)|rebaj(?:a|e|ar|émosla)|'
        r'si+|ok+|dale+|claro|obvio|ya\s*po|vámonos|vamos|adelante|'
        r'autoriz[ao]|proced(?:an?|amos)|hagamos|confirmo|perfecto|listo|hecho|'
        r'cuenta\s*conmigo|dale\s*no\s*m[áa]s|ya\s*bajen|s[iíí]+\s*po|'
        r'7\%|10\%|15\%|20\%|baja|rebaja|ajuste)\b',
        re.IGNORECASE
    )

    if REGEX_BAJA.search(texto):
        ya_autorizo_antes = contacto.get("autoriza_baja", False)
        accion = "autoriza_baja_automatica"
        score = 10

        if ya_autorizo_antes:
            respuesta = f"¡De nada {primer_nombre}! 😊\n\nYa tenemos todo listo para bajar el precio y venderla rápido.\nEn máximo 48 hrs verás los cambios publicados.\n¡Vamos con todo! 🔥"
        else:
            respuesta = RESPONSES_PROPIETARIO["autoriza_baja"].format(primer_nombre=primer_nombre)

    # ===================================================================
    # 2. RECHAZO AGRESIVO (PRIMERO: no me molesten, spam, denunciar → cierre definitivo)
    # ===================================================================
    elif re.search(r'\b(no\s*molest|spam|denunci|bloqu|acoso|insist|basta|'
                   r'déjame\s*en\s*paz|cállate|para\s*ya|no\s*contact|molestando|'
                   r'déjame\s*tranquilo|no\s*insist|no\s*llamen)\b', texto, re.IGNORECASE):
        respuesta = RESPONSES_PROPIETARIO["rechazo_agresivo"].format(primer_nombre=primer_nombre)
        accion = "rechazo_agresivo"
        score = 1

    # ===================================================================
    # 3. RECHAZO A LA BAJA DE PRECIO (vale más, estás loco, no acepto, etc.)
    # ===================================================================
    elif re.search(r'\b(vale\s+m[áa]s|est[áa]s?\s*loco|rid[ií]culo|muy\s+bajo|'
                   r'no\s+acepto|no\s+estoy\s+de\s+acuerdo|mi\s+(casa|depto|propiedad)\s+vale\s+m[áa]s|'
                   r'tasaci[óo]n\s+errada|inaceptable|exagerado|no\s+me\s+interesa\s+la\s+baja|'
                   r'no\s+quiero\s+bajar|negativo)\b', texto, re.IGNORECASE):
        respuesta = RESPONSES_PROPIETARIO["rechaza_baja"].format(primer_nombre=primer_nombre)
        accion = "rechaza_baja_precio"
        score = 6

    # ===================================================================
    # 4. MANTIENE PRECIO
    # ===================================================================
    elif re.search(r'\b(2|dos|mantener|esperar|no\s+bajar|seguir\s*igual|'
                   r'dejar\s*as[ií]|todav[íi]a\s*no|por\s*ahora\s*no)\b', texto, re.IGNORECASE):
        respuesta = RESPONSES_PROPIETARIO["mantiene"].format(primer_nombre=primer_nombre)
        accion = "mantiene_precio"
        score = 5

    # ===================================================================
    # 5. PAUSA REAL (solo cuando pide explícitamente)
    # ===================================================================
    elif re.search(r'\b(3|tres|pausa|retirar|quitar|sacar|no\s+disponible|'
                   r'no\s+vender|para\s*despu[ée]s|ya\s*no\s*vendo)\b', texto, re.IGNORECASE):
        respuesta = RESPONSES_PROPIETARIO["pausa"].format(primer_nombre=primer_nombre)
        accion = "pausa_venta"
        score = 2

    # ===================================================================
    # 6. CUALQUIER OTRA COSA → fallback Grok (actualizado con nuevas clases)
    # ===================================================================
    else:
        try:
            from chatbot import call_grok
            prompt = f"""Clasifica SOLO con una palabra: BAJA, AGRESIVO, RECHAZA, MANTIENE, PAUSA o CALIENTE.

Mensaje: "{original}"

Responde SOLO la palabra."""
            grok_resp = call_grok(prompt, temperature=0.0, max_tokens=10)
            clasif = grok_resp.strip().upper() if grok_resp else "CALIENTE"

            if clasif in ["BAJA", "1"]:
                respuesta = RESPONSES_PROPIETARIO["autoriza_baja"].format(primer_nombre=primer_nombre)
                accion = "autoriza_baja_automatica"
                score = 10
            elif clasif == "AGRESIVO":
                respuesta = RESPONSES_PROPIETARIO["rechazo_agresivo"].format(primer_nombre=primer_nombre)
                accion = "rechazo_agresivo"
                score = 1
            elif clasif == "RECHAZA":
                respuesta = RESPONSES_PROPIETARIO["rechaza_baja"].format(primer_nombre=primer_nombre)
                accion = "rechaza_baja_precio"
                score = 6
            elif clasif == "MANTIENE":
                respuesta = RESPONSES_PROPIETARIO["mantiene"].format(primer_nombre=primer_nombre)
                accion = "mantiene_precio"
                score = 5
            elif clasif == "PAUSA":
                respuesta = RESPONSES_PROPIETARIO["pausa"].format(primer_nombre=primer_nombre)
                accion = "pausa_venta"
                score = 2
            else:
                respuesta = RESPONSES_PROPIETARIO["default_caliente"].format(primer_nombre=primer_nombre, codigo=codigo)
                accion = "respuesta_caliente"
                score = 8

        except Exception as e:
            print(f"[FALLBACK PROPIETARIO] Error Grok: {e}")
            respuesta = RESPONSES_PROPIETARIO["default_caliente"].format(primer_nombre=primer_nombre, codigo=codigo)
            accion = "fallback_error"
            score = 7

    # ===================================================================
    # GUARDAR EN MONGO (con motivo_desactivacion)
    # ===================================================================
    desactivar = accion in ["pausa_venta", "rechazo_agresivo"]
    motivo = None
    if desactivar:
        motivo = "pausa_voluntaria" if accion == "pausa_venta" else "rechazo_agresivo"

    contactos_collection.update_one(
        {"telefono": phone},
        {"$set": {
            "clasificacion_propietario": accion,
            "ultima_respuesta": original,
            "fecha_clasificacion": datetime.now(timezone.utc),
            "autoriza_baja": accion == "autoriza_baja_automatica",
            "activo": not desactivar,
            "motivo_desactivacion": motivo
        },
         "$push": {"messages": {"$each": [
             {"role": "user", "content": original, "timestamp": datetime.now(timezone.utc)},
             {"role": "assistant", "content": respuesta, "timestamp": datetime.now(timezone.utc), "metadata": {"accion": accion, "score": score}}
         ]}}}
    )

    print(f"[PROPIETARIO] {phone} → {accion} (score {score}) {'[DESACTIVADO]' if desactivar else ''}")
    return respuesta

"""
    # ===================================================================
    # EMAIL solo a los que autorizan o están calientes
    # ===================================================================
    if enviar_email:
        try:
            from email_utils import send_gmail_alert
            titulo = "AUTORIZÓ BAJA" if "autoriza_baja" in accion else "PROPIETARIO CALIENTE"
            send_gmail_alert(
                phone=phone,
                lead_type=f"PROPIETARIO 🔥 {titulo}",
                lead_score=score,
                criteria={"codigo": codigo, "nombre": nombre_completo, "accion": accion},
                last_user_msg=original,
                last_response=respuesta
            )
        except Exception as e:
            print(f"[ERROR EMAIL] {e}")

    return respuesta
"""
# ===================================================================
# HUMANIZAR RESPUESTA CON GROK (usa campos truncados → pocos tokens)
# ===================================================================
def humanizar_con_grok(respuesta_robot: str, criteria: dict, history: List[Dict]) -> str:
    contexto = " | ".join([m["content"] for m in history[-6:] if m.get("role") == "user"][-3:])

    prompt = f"""
Eres una ejecutiva senior de Procasa Jorge Pablo Caro Propiedades: profesional, cálido y muy efectivo en WhatsApp.
Hablas con respeto, confianza y calidez chilena suave (nada de groserías).

Lo que el cliente busca: {criteria}
Últimos mensajes del cliente: {contexto}

Tu misión: convertir este mensaje robótico lleno de bullet points en un relato 100% natural, cálido y conversacional, como si se lo estuvieras contando a un cliente importante por WhatsApp.

Texto robótico que debes transformar (usa toda esta info, pero NUNCA copies textual las líneas que digan "Imagen:" ni "Amenities:" ni "Ubicación:"):

{respuesta_robot}

Reglas de oro:
- Integra la descripción de las fotos y los amenities de forma natural dentro del relato (ej: "la foto del living es increíble, se ve súper luminoso con esa vista al jardín")
- Nunca digas "Imagen:", "Amenities:" ni "Ubicación:" → eso queda robótico y está prohibido
- Termina siempre invitando a la acción de forma clara y profesional: "¿Cuál te gustó más?", "¿Agendamos visita?"
- Máximo 2 frases cortas por propiedad (máximo 110 caracteres en total por una)

Responde SOLO el texto natural, sin json, sin código, sin comillas.
"""

    try:
        from chatbot import call_grok
        resp = call_grok(prompt, temperature=0.75)
        if resp and len(resp.strip()) > 100:
            return resp.strip()
    except:
        pass
    return respuesta_robot  # fallback solo si falla todo

def handle_continue(phone: str, user_msg: str, history: List[Dict[str, Any]], tipo_contacto: str, contactos_collection, responses=None):
    responses = responses or RESPONSES
    contacto = contactos_collection.find_one({"telefono": phone})
    criteria: Dict[str, Any] = (contacto.get("criteria", {}) if contacto else {}).copy()

    lower_msg = user_msg.lower().strip()

    # 1. Escalado rápido si pide visita o asesor
    if any(palabra in lower_msg for palabra in ["visita", "ver", "agendar", "llamar", "asesor", "ejecutivo", "hablar con persona"]):
        from handlers import handle_advisor
        return handle_advisor(phone, user_msg, history, tipo_contacto, contactos_collection, responses)

    # 2. Stop explícito
    if any(frase in lower_msg for frase in ["no estoy buscando", "no busco", "equivocado", "no me interesa", "no quiero", "error"]):
        from handlers import handle_stop
        return handle_stop(phone, user_msg, tipo_contacto, contactos_collection, responses, deactivate_contacto)

    # 3. Extraer y ACUMULAR criterios nuevos
    nuevos = extract_criteria(user_msg, history)
    print(f"[DEBUG] Nuevos criterios: {nuevos}")

    for key, value in nuevos.items():
        if value and value not in [None, "", [], "null"]:
            if key == "comuna" and isinstance(value, list):
                existing = criteria.get("comuna", [])
                if isinstance(existing, str):
                    existing = [existing]
                criteria["comuna"] = list(set(existing + value))
            else:
                criteria[key] = value

    print(f"[DEBUG] Criterios acumulados: {criteria}")

    # Guardar criterios
    contactos_collection.update_one(
        {"telefono": phone},
        {"$set": {"criteria": criteria}},
        upsert=True
    )

    # 4. ¿Ya tiene los 3 campos clave?
    tiene_todos = (
        bool(criteria.get("operacion")) and
        bool(criteria.get("tipo")) and
        bool(criteria.get("comuna")) and (
            isinstance(criteria["comuna"], str) or 
            (isinstance(criteria["comuna"], list) and len(criteria["comuna"]) > 0)
        )
    )

    if tiene_todos:
        ya_vistas = contacto.get("propiedades_mostradas", []) if contacto else []
        props = buscar_propiedades(criteria, limit=3, ya_vistas=ya_vistas)

        if props:
            codigos = [p["codigo"] for p in props]

            # Mensaje base con campos ricos
            base = "¡Te encontré unas excelentes opciones! 🔥\n\n"
            for p in props:
                desc = (p.get("descripcion_clean") or "")[:200]
                img = (p.get("image_text") or "")[:150]
                amen = (p.get("amenities_text") or "")[:150]
                precio = p.get("precio_uf", "?")
                comuna = p.get("comuna", "?")
                tipo = p.get("tipo", "Propiedad")
                codigo = p.get("codigo", "000")

                base += f"• {tipo} en {comuna}\n"
                if desc: base += f"  {desc}\n"
                if img: base += f"  {img}\n"
                if amen: base += f"  {amen}\n"
                base += f"  💰 {precio} UF\n"
                base += f"  🔗 https://www.procasa.cl/{codigo}\n\n"

            base += "¿Agendamos visita? 🚗\n'deseas más opciones'"

            respuesta = humanizar_con_grok(base, criteria, history)

            # Guardar vistas
            contactos_collection.update_one(
                {"telefono": phone},
                {"$addToSet": {"propiedades_mostradas": {"$each": codigos}}}
            )
        else:
            respuesta = "No tennemos opciones nuevas con esos filtros 😔\n¿Quieres que busque en comunas cercanas o suba un poco el presupuesto?"
    else:
        faltan = []
        if not criteria.get("operacion"): faltan.append("¿compra o arriendo?")
        if not criteria.get("tipo"): faltan.append("¿casa, depto, oficina, local?")
        if not criteria.get("comuna"): faltan.append("¿en qué comuna(s)? (puedes decir varias)")

        #respuesta = f"¡Perfecto! Ya casi estamos 😊\nSolo me falta saber:\n• {'\n• '.join(faltan)}\n¡Y te muestro lo mejor al tiro!"
        respuesta = (
    "¡Perfecto! Ya casi estamos 😊\n"
    "Solo me falta saber:\n"
    "• " + "\n• ".join(faltan) + "\n"
    "¡Y te muestro lo mejor al tiro!"
)

    # Guardar conversación
    metadata = {"intention": "continue", "action": "propiedades_ofrecidas" if tiene_todos else "recoleccion"}
    contactos_collection.update_one(
        {"telefono": phone},
        {"$push": {"messages": {"$each": [
            {"role": "user", "content": user_msg, "timestamp": datetime.now(timezone.utc)},
            {"role": "assistant", "content": respuesta, "timestamp": datetime.now(timezone.utc), "metadata": metadata}
        ]}}}
    )
    return respuesta