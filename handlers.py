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
        criteria = contacto.get("criteria", {}) if contacto else {}
        full_history = contacto.get("messages", []) if contacto and isinstance(contacto.get("messages"), list) else []

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

def handle_propietario_respuesta(phone: str, user_msg: str, contacto: dict, contactos_collection) -> str:
    original = user_msg.strip()
    texto = original.lower().strip()

    nombre_raw = contacto.get("nombre_propietario") or contacto.get("nombre") or "Cliente"
    primer_nombre = nombre_raw.split(maxsplit=1)[0].title()
    codigos = [contacto.get("codigo")] if contacto.get("codigo") else []
    if not codigos:
        codigos = [doc.get("codigo") for doc in contactos_collection.find({"telefono": phone}, {"codigo": 1}) if doc.get("codigo")]

    # Cargar datos reales de todas sus propiedades
    datos_propiedades = {}
    calculos_7pct = []
    for cod in codigos:
        if cod:
            info = cargar_datos_propiedad(cod)
            if info:
                datos_propiedades[cod] = info
                if info["precio_uf_actual"]:
                    nuevo = round(info["precio_uf_actual"] * 0.93, 1)
                    calculos_7pct.append(f"{cod} ({info['comuna']}): {info['precio_uf_actual']} → {nuevo} UF")

    # Historial para Grok
    messages = contacto.get("messages", [])
    historial = "\n".join([f"{m.get('role','?')}: {m.get('content','')}" for m in messages[-10:]])
    ya_autorizo = any("excelente decisión" in m.get("content", "").lower() for m in messages if m.get("role") == "assistant")

    # Regex ultra-precisos (solo lo 100% claro)
    ACEPTA_CLARO = re.compile(r'\b(1\b|opci[oó]n\s*1|uno|s[íií]+\b|ok+\b|dale+\b|adelante\b|perfecto\b|confirm[ao]\b|autoriz[ao]\b|proced[ae]\b|hecho\b|listo\b)\b', re.I)
    MANTIENE_CLARO = re.compile(r'\b(2\b|opci[oó]n\s*2|dos|mantener\b|prefiero\s*mantener)\b', re.I)
    PAUSA_CLARO = re.compile(r'\b(3\b|opci[oó]n\s*3|tres|ya\s*(vend|arriend)|retirar|pausa|sacar|no\s*disponible|bajar\s*publicaci[óo]n)\b', re.I)
    RECHAZO_AGRESIVO = re.compile(r'\b(spam|denunci|bloque|acoso|basta|para\s*ya|déjame\s*en\s*paz|borra|elimina|sácame|sernac|molest|hincha|pesado|insistiendo|cortala)\b', re.I)

    respuesta = ""
    accion = "continua_con_grok"
    score = 8

    # 1. Rechazo agresivo → vetar inmediatamente
    if RECHAZO_AGRESIVO.search(texto):
        respuesta = f"Lamento profundamente si el mensaje fue inoportuno, {primer_nombre}. He eliminado tu número de todas nuestras campañas. No recibirás más comunicaciones. Que tengas buena jornada."
        accion = "rechazo_agresivo"
        score = 1
        contactos_collection.update_one({"telefono": phone}, {"$set": {"activo": False}})

    # 2. Pausa clara
    elif PAUSA_CLARO.search(texto):
        respuesta = RESPONSES_PROPIETARIO["pausa"].format(primer_nombre=primer_nombre)
        accion = "pausa_venta"
        score = 2

    # 3. Acepta claramente (solo la primera vez)
    elif not ya_autorizo and ACEPTA_CLARO.search(texto):
        respuesta = RESPONSES_PROPIETARIO["autoriza_baja"].format(primer_nombre=primer_nombre)
        accion = "autoriza_baja_automatica"
        score = 10

    # 4. Mantiene precio claramente
    elif MANTIENE_CLARO.search(texto):
        respuesta = RESPONSES_PROPIETARIO["mantiene"].format(primer_nombre=primer_nombre)
        accion = "mantiene_precio"
        score = 5

    # 5. Todo lo demás → Grok con datos 100% reales
    else:
        datos_str = "No se encontraron datos de la propiedad."
        if datos_propiedades:
            datos_str = "\n".join([
                f"• Código {cod}: {info['tipo']} en {info['comuna']}, {info['precio_uf_actual'] or '?'} UF"
                + (f", {info['dormitorios']} dorm, {info['m2_util']} m² útiles" if info.get('dormitorios') else "")
                for cod, info in datos_propiedades.items()
            ])

        prompt = f"""
        Eres Bernardita, ejecutiva senior de Procasa Jorge Pablo Caro Propiedades.
        Tono profesional, cálido y elegante. Hablas con respeto y claridad.

        Datos reales de la(s) propiedad(es):
        {datos_str}

        Cálculo exacto del -7%:
        {chr(10).join(calculos_7pct) if calculos_7pct else "No disponible"}

        Links directos (úsalos siempre cuando menciones una propiedad):
        {chr(10).join([f"• Código {cod}: https://www.procasa.cl/{cod}" for cod in datos_propiedades.keys()] if datos_propiedades else "No disponible")}

        Historial:
        {historial}

        Mensaje del propietario:
        {original}

        Reglas clave:
        - Si pregunta “¿cuál propiedad?” o “¿de qué hablas?” → responde con código, tipo, comuna, precio actual y el link directo.
        - Siempre incluye el link https://www.procasa.cl/CÓDIGO cuando hagas referencia a la propiedad.
        - Si pregunta cuánto quedaría → usa el cálculo exacto de arriba.
        - Máximo 5 líneas. Termina invitando a confirmar el ajuste.

        Responde SOLO el texto para WhatsApp.
        """

        from chatbot import call_grok
        respuesta_grok = call_grok(prompt, temperature=0.3, max_tokens=800)
        respuesta = respuesta_grok.strip() if respuesta_grok else RESPONSES_PROPIETARIO["default_caliente"].format(primer_nombre=primer_nombre, codigo=codigos[0] if codigos else "tu propiedad")

    # Guardar en Mongo
    update_data = {
        "campanas.data_dura_7pct.ultima_accion": accion,
        "campanas.data_dura_7pct.score": score,
        "campanas.data_dura_7pct.fecha_respuesta": datetime.now(timezone.utc),
        "clasificacion_propietario": accion,
        "autoriza_baja": accion in ["autoriza_baja_automatica", "baja_aceptada_grok"]
    }

    contactos_collection.update_one(
        {"telefono": phone},
        {"$set": update_data,
         "$push": {"messages": {"$each": [
             {"role": "user", "content": original},
             {"role": "assistant", "content": respuesta, "metadata": {"accion": accion, "score": score}}
         ]}}}
    )

    # Email si es caliente o autoriza
    if score >= 8 or "autoriza" in accion or "baja" in accion:
        try:
            from email_utils import send_propietario_alert
            send_propietario_alert(
                phone=phone,
                nombre=nombre_raw,
                codigos=codigos,
                mensaje_original=original,
                accion_detectada=accion,
                respuesta_bot=respuesta,
                autoriza_baja="autoriza" in accion or "baja" in accion
            )
        except Exception as e:
            print(f"[ERROR EMAIL PROPIETARIO] {e}")

    return respuesta

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
- Máximo 2 frases cortas por propiedad (máximo 90 caracteres en total por una)

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

        respuesta = (
    "¡Perfecto! Ya casi estamos 😊\n"
    "Solo me falta saber:\n"
    "• " + "\n• ".join(faltan) + "\n"
    "¡Y te muestro lo mejor de inmediato!"
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

# ===================================================================
# NUEVA FUNCIÓN: Cargar datos reales desde universo_obelix
# ===================================================================
def cargar_datos_propiedad(codigo: str) -> dict:
    """Busca en universo_obelix por código y devuelve datos limpios y útiles"""
    if not codigo:
        return {}

    from config import Config
    from pymongo import MongoClient
    config = Config()
    client = MongoClient(config.MONGO_URI)
    db = client[config.DB_NAME]

    prop = db["universo_obelix"].find_one(
        {"codigo": codigo},
        {
            "tipo_propiedad": 1,
            "comuna": 1,
            "precio_uf": 1,
            "precio_clp": 1,
            "operacion": 1,
            "dormitorios": 1,
            "banos": 1,
            "superficie_util": 1,
            "superficie_total": 1,
            "calle_referencia_1": 1,
            "calle_referencia_2": 1,
            "descripcion": 1
        }
    )

    if not prop:
        return {"codigo": codigo, "comuna": "No encontrada", "precio_uf_actual": None}

    precio_uf = prop.get("precio_uf")
    if isinstance(precio_uf, dict):
        precio_uf = next(iter(precio_uf.values()), None) if precio_uf else None

    return {
        "codigo": codigo,
        "tipo": str(prop.get("tipo_propiedad", "Propiedad")).split()[0].title(),
        "comuna": str(prop.get("comuna", "Santiago")).title(),
        "precio_uf_actual": float(precio_uf) if precio_uf else None,
        "precio_clp": prop.get("precio_clp"),
        "operacion": str(prop.get("operacion", "Venta")).title(),
        "dormitorios": prop.get("dormitorios"),
        "banos": prop.get("banos"),
        "m2_util": prop.get("superficie_util"),
        "m2_total": prop.get("superficie_total"),
        "calle1": prop.get("calle_referencia_1"),
        "calle2": prop.get("calle_referencia_2"),
        "descripcion": str(prop.get("descripcion", "") or "")[:400]
    }