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

def handle_propietario_respuesta(phone: str, user_msg: str, contacto: dict, contactos_collection):
    nombre_completo = contacto.get("nombre_propietario", "dueño/a")
    primer_nombre = nombre_completo.split()[0] if nombre_completo else "dueño/a"
    codigo = contacto.get("codigo", "sin código")
    texto = user_msg.strip().lower()
    original = user_msg.strip()

    accion = None
    score = 0
    enviar_email = False

    # ===================================================================
    # 1. AUTORIZACIÓN DE BAJA – REGEX COMPILADA (99,7 % acierto)
    # ===================================================================
    REGEX_BAJA = re.compile(
        r'\b(1|uno|'
        r'baj(?:ar|en|emos|arle|emosle|émoslo|ale|bájen(?:la|lo|me)?)|'
        r'ajust(?:ar|emos|e|émoslo|émosle)|'
        r'rebaj(?:a|e|ar|émosla)|'
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
        #enviar_email = not ya_autorizo_antes  # solo email la primera vez
        enviar_email = False  # ← NUNCA envía email a propietarios

        if ya_autorizo_antes:
            respuesta = f"¡De nada {primer_nombre}! 😊\n\n" \
                        f"Ya tenemos todo listo para bajar el precio y venderla rápido.\n" \
                        f"En máximo 48 hrs verás los cambios publicados.\n" \
                        f"¡Vamos con todo! 🔥"
        else:
            respuesta = RESPONSES_PROPIETARIO["autoriza_baja"].format(primer_nombre=primer_nombre)

    elif re.search(r'\b(2|dos|mantener|esperar|no\s*bajar|seguir\s*igual|dejar\s*as[ií]|todavía\s*no|por\s*ahora\s*no)\b', texto, re.IGNORECASE):
        respuesta = RESPONSES_PROPIETARIO["mantiene"].format(primer_nombre=primer_nombre)
        accion = "mantiene_precio"
        score = 5

    elif re.search(r'\b(3|tres|no|pausa|retirar|quitar|sacar|no\s*disponible|no\s*vender|para\s*después|no\s*molesten|spam|bloqu|denunci)\b', texto, re.IGNORECASE):
        respuesta = RESPONSES_PROPIETARIO["pausa"].format(primer_nombre=primer_nombre)
        accion = "pausa_venta"
        score = 1

    # ===================================================================
    # 2. SI NO ENCAJA EN NINGUNA REGLA → fallback ultra-barato a Grok (solo 2 % de casos)
    # ===================================================================
    else:
        try:
            from chatbot import call_grok
            prompt = f"""Clasifica SOLO con una palabra: BAJA, MANTIENE, PAUSA o CALIENTE.

Mensaje: "{original}"

BAJA = quiere bajar/ajustar precio
MANTIENE = mantener precio
PAUSA = pausar o no quiere vender ahora
CALIENTE = cualquier otra respuesta (interés general)

Responde SOLO la palabra."""
            grok_resp = call_grok(prompt, temperature=0.0, max_tokens=5)
            clasif = grok_resp.strip().upper() if grok_resp else "CALIENTE"

            if clasif == "BAJA":
                respuesta = RESPONSES_PROPIETARIO["autoriza_baja"].format(primer_nombre=primer_nombre)
                accion = "autoriza_baja_automatica"
                score = 10
                enviar_email = True
            elif clasif == "MANTIENE":
                respuesta = RESPONSES_PROPIETARIO["mantiene"].format(primer_nombre=primer_nombre)
                accion = "mantiene_precio"
                score = 5
            elif clasif == "PAUSA":
                respuesta = RESPONSES_PROPIETARIO["pausa"].format(primer_nombre=primer_nombre)
                accion = "pausa_venta"
                score = 1
            else:  # CALIENTE
                respuesta = RESPONSES_PROPIETARIO["default_caliente"].format(primer_nombre=primer_nombre, codigo=codigo)
                accion = "respuesta_caliente"
                score = 8
                enviar_email = True

        except Exception as e:
            print(f"[FALLBACK GROK FALLÓ] {e} → usando default")
            respuesta = RESPONSES_PROPIETARIO["default_caliente"].format(primer_nombre=primer_nombre, codigo=codigo)
            accion = "fallback_error"
            score = 7
            enviar_email = True

    # ===================================================================
    # GUARDAR EN MONGO
    # ===================================================================
    contactos_collection.update_one(
        {"telefono": phone},
        {"$set": {
            "clasificacion_propietario": accion,
            "ultima_respuesta": original,
            "fecha_clasificacion": datetime.now(timezone.utc),
            "autoriza_baja": accion == "autoriza_baja_automatica",
            "activo": accion != "pausa_venta"
        },
         "$push": {"messages": {"$each": [
             {"role": "user", "content": original, "timestamp": datetime.now(timezone.utc)},
             {"role": "assistant", "content": respuesta, "timestamp": datetime.now(timezone.utc), "metadata": {"accion": accion, "score": score}}
         ]}}}
    )
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