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

    # === OBTENER CÓDIGOS (puede tener varios o ninguno) ===
    codigos = [contacto.get("codigo")] if contacto.get("codigo") else []
    if not codigos:
        codigos = [doc.get("codigo") for doc in contactos_collection.find({"telefono": phone}, {"codigo": 1}) if doc.get("codigo")]

    # === CARGAR DATOS REALES DE universo_obelix ===
    datos_propiedades = {}
    for cod in codigos:
        if cod:
            info = cargar_datos_propiedad(cod)
            if info:
                datos_propiedades[cod] = info

    # === CONSTRUCCIÓN 100% SEGURA DE TEXTOS PARA GROK ===
    datos_lines = []
    calculos_7pct = []
    links_lines = []

    for cod, info in datos_propiedades.items():
        if info.get("error"):
            datos_lines.append(f"• Código {cod}: No encontrado en base de datos")
            continue

        tipo = info.get("tipo", "Propiedad")
        comuna = info.get("comuna", "?")
        precio = info.get("precio_uf_actual")
        precio_txt = f"{precio:,.0f} UF".replace(",", ".") if precio else "sin precio"

        extra = []
        if info.get("dormitorios"): extra.append(f"{info['dormitorios']} dorm")
        if info.get("m2_util"): extra.append(f"{info['m2_util']} m² útiles")
        extra_str = f" ({', '.join(extra)})" if extra else ""

        datos_lines.append(f"• Código {cod}: {tipo} en {comuna}, {precio_txt}{extra_str}")
        links_lines.append(f"• https://www.procasa.cl/{cod}")

        if precio and precio > 0:
            nuevo = round(precio * 0.93, 1)
            calculos_7pct.append(f"{cod} ({comuna}): {precio:,.0f} → {nuevo:,.1f} UF".replace(",", "."))

    datos_str = "\n".join(datos_lines) if datos_lines else "No se encontraron datos de la propiedad."
    links_str = "\n".join(links_lines) if links_lines else "No disponible"

    # === HISTORIAL Y ESTADO ===
    messages = contacto.get("messages", [])
    historial = "\n".join([f"{m.get('role','?')}: {m.get('content','')}" for m in messages[-10:]])
    ya_autorizo = any("excelente decisión" in m.get("content", "").lower() for m in messages if m.get("role") == "assistant")

    # === ANTI-SPAM DE EMAILS (solo 1 por contacto en esta campaña) ===
    campana_data = contacto.get("campanas", {}).get("data_dura_7pct", {})
    if campana_data.get("email_enviado", False):
        enviar_email = False
    else:
        enviar_email = True
        contactos_collection.update_one(
            {"telefono": phone},
            {"$set": {"campanas.data_dura_7pct.email_enviado": True}}
        )

    # === REGEX ULTRA-PRECISES ===
    ACEPTA_CLARO = re.compile(r'\b(1|opci[oó]n\s*1|uno|s[íií]+|ok+|dale+|adelante|perfecto|confirm[ao]|autoriz[ao]|proced[ae]|hecho|listo)\b', re.I)
    MANTIENE_CLARO = re.compile(r'\b(2|opci[oó]n\s*2|dos|mantener|prefiero\s*mantener)\b', re.I)
    PAUSA_CLARO = re.compile(r'\b(3|opci[oó]n\s*3|tres|ya\s*(vend|arriend)|retirar|pausa|sacar|no\s*disponible|bajar\s*publicaci[óo]n)\b', re.I)
    RECHAZO_AGRESIVO = re.compile(r'\b(spam|denunci|bloque|acoso|basta|para\s*ya|déjame\s*en\s*paz|borra|elimina|sácame|sernac|molest|hincha|pesado|insistiendo|cortala)\b', re.I)

    respuesta = ""
    accion = "continua_con_grok"
    score = 8

    # 1. Rechazo agresivo → vetar
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

    # 5. Todo lo demás → Grok con datos reales + links + cálculo exacto
    else:
        prompt = f"""
Eres Bernardita, ejecutiva senior de Procasa Jorge Pablo Caro Propiedades.
Tono profesional, cálido, respetuoso y elegante. Nunca uses "tinca", "ya po", "volamos".

Datos reales de la(s) propiedad(es):
{datos_str}

Cálculo exacto del -7%:
{chr(10).join(calculos_7pct) if calculos_7pct else "No disponible"}

Links directos:
{links_str}

Historial de la conversación:
{historial}

Mensaje actual del propietario:
{original}

Instrucciones:
- Usa siempre los datos reales de arriba.
- Si pregunta "¿cuál propiedad?" → responde con código, tipo, comuna, precio y link.
- Si pregunta "¿cuánto quedaría?" → responde con el cálculo exacto del 7%.
- Máximo 5 líneas. Termina invitando a confirmar o preguntando cómo prefiere avanzar.
- Responde SOLO el texto natural para WhatsApp.
"""

        from chatbot import call_grok
        respuesta_grok = call_grok(prompt, temperature=0.3, max_tokens=800)
        respuesta = respuesta_grok.strip() if respuesta_grok else RESPONSES_PROPIETARIO["default_caliente"].format(primer_nombre=primer_nombre, codigo=codigos[0] if codigos else "tu propiedad")

    # === GUARDAR EN MONGO ===
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

    # === EMAIL → SOLO UNA VEZ POR CONTACTO ===
    if enviar_email and (score >= 8 or "autoriza" in accion or "baja" in accion):
        try:
            from email_utils import send_propietario_alert
            send_propietario_alert(
                phone=phone,
                nombre=nombre_raw,
                codigos=codigos or ["sin código"],
                mensaje_original=original,
                accion_detectada=accion,
                respuesta_bot=respuesta,
                autoriza_baja=("autoriza" in accion or "baja" in accion)
            )
            print(f"[EMAIL ENVIADO 1 VEZ] {phone} → {accion}")
        except Exception as e:
            print(f"[ERROR EMAIL] {e}")

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
# FUNCIÓN DEFINITIVA → CARGA DATOS REALES DESDE universo_obelix (2025)
# ===================================================================
def cargar_datos_propiedad(codigo: str) -> dict:
    """Busca en universo_obelix por código y devuelve datos 100% reales y seguros"""
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
            "tipo": 1,
            "comuna": 1,
            "precio": 1,           # ← este es el precio en UF (entero)
            "precio_uf": 1,         # ← a veces viene como dict, a veces no
            "precio_clp": 1,
            "operacion": 1,
            "dormitorios": 1,
            "banos": 1,
            "m2_utiles": 1,
            "m2_totales": 1,
            "calle_referencia_1": 1,
            "calle_referencia_2": 1,
            "descripcion": 1,
            "descripcion_clean": 1
        }
    )

    if not prop:
        return {"codigo": codigo, "error": "Propiedad no encontrada en universo_obelix"}

    # === PRECIO EN UF: puede venir de varias formas ===
    precio_uf_raw = prop.get("precio") or prop.get("precio_uf")
    if isinstance(precio_uf_raw, dict):
        precio_uf = next((v for v in precio_uf_raw.values() if v), None)
    else:
        precio_uf = precio_uf_raw

    try:
        precio_uf = float(precio_uf) if precio_uf else None
    except:
        precio_uf = None

    return {
        "codigo": codigo,
        "tipo": str(prop.get("tipo", "Propiedad")).split()[0].title() if prop.get("tipo") else "Propiedad",
        "comuna": str(prop.get("comuna", "Santiago")).title(),
        "precio_uf_actual": precio_uf,
        "precio_clp": prop.get("precio_clp"),
        "operacion": str(prop.get("operacion", "Venta")).title(),
        "dormitorios": prop.get("dormitorios"),
        "banos": prop.get("banos"),
        "m2_util": prop.get("m2_utiles"),
        "m2_total": prop.get("m2_totales"),
        "calle1": prop.get("calle_referencia_1"),
        "calle2": prop.get("calle_referencia_2"),
        "descripcion": (prop.get("descripcion_clean") or prop.get("descripcion") or "")[:400]
    }