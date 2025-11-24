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
    contacto = contacto or {}
    
    nombre_raw = contacto.get("nombre_propietario") or contacto.get("nombre") or "Propietario"
    primer_nombre = nombre_raw.strip().split(maxsplit=1)[0].title() if nombre_raw.strip() else "Propietario"
    codigo = contacto.get("codigo", "sin código")

    original = user_msg.strip()
    texto = original.lower().strip()

    # Variables para guardar al final
    respuesta = ""
    accion = "sin_clasificar"
    score = 5
    estado_campana = "pendiente"
    desactivar = False
    motivo_desactivacion = None

    # ===================================================================
    # 1. AUTORIZA BAJA (OPCIÓN 1) – PRIORIDAD MÁXIMA
    # ===================================================================
    if re.search(r'\b(1\s*(️⃣|$|\b)|uno|un[ao]?\b|'
                 r'baj(?:ar|en|emos|arle|émoslo|ale|ita|bájen(?:la|lo|me)?)\b|'
                 r'rebaj(?:a|e|ar|émosla|ita|émoslo|en|émosle)\b|'
                 r'ajust(?:ar|emos|e|émoslo|émosle|ito|en)\b|'
                 r'modific(?:ar|a|an|alo|al[ao])\b|'
                 r'cambi(?:ar|emos|a|alo)\s*(?:el\s*)?(?:precio|valor)\b|'
                 r'baja|rebaja|ajuste|reducción|descuento|menor|menos|'
                 r's[ií]+|ok+|dale+|claro|obvio|ya\s*po|vamos|adelante|proced(?:a|e)|'
                 r'hag[áa]moslo|me\s*parece\s*bien|bueno|autoriz|confirmo|acepto|'
                 r'apruebo|perfecto|listo|hecho|opci[óo]n\s*1\b|'
                 r'(?:5|6|7|8|9|10|12|15|20|25)\s*(%|por\s*ciento|puntos?)\b)', texto, re.IGNORECASE):

        accion = "autoriza_baja_automatica"
        score = 10
        estado_campana = "baja_autorizada"
        respuesta = f"¡Excelente decisión, {primer_nombre}!\n\n" \
                    f"Ya programé el ajuste del precio para que tu propiedad entre en el rango de los pocos créditos que están aprobando hoy.\n" \
                    f"En máximo 72 horas verás el nuevo valor publicado en todos los portales.\n\n" \
                    f"¡Vamos con todo a cerrar esta venta rápido!"

    # ===================================================================
    # 2. RECHAZO AGRESIVO → VETAR INMEDIATO
    # ===================================================================
    elif re.search(r'\b(no\s*molest|spam|denunci|bloqu|acoso|basta|para\s*ya|'
                   r'd[ée]jame\s*en\s*paz|c[áa]llate|no\s*contact|molestando|insist|'
                   r'borr[ao]|elimin[ao]|sacame|s[áa]came|sernac|polic[íi]a|demand|'
                   r'qué\s*parte\s*de\s*no|hincha|pesado|cortala)\b', texto, re.IGNORECASE):
        accion = "rechazo_agresivo"
        score = 1
        estado_campana = "vetado"
        desactivar = True
        motivo_desactivacion = "rechazo_agresivo"
        respuesta = f"Lamento mucho si el mensaje fue inoportuno, {primer_nombre}. Ya eliminé tu número de todas nuestras campañas automáticas. No volverás a recibir mensajes de este tipo."

    # ===================================================================
    # 3. RECHAZA LA BAJA (pero sin enojarse)
    # ===================================================================
    elif re.search(r'\b(vale\s+m[áa]s|est[áa]s?\s*loco|rid[ií]culo|muy\s+bajo|'
                   r'no\s+acepto|inaceptable|exagerado|regal(?:ar|o)|botar|'
                   r'ni\s*cag|desvaloriz|pierd[oa])\b', texto, re.IGNORECASE):
        accion = "rechaza_baja_precio"
        score = 6
        estado_campana = "rechaza_baja"
        respuesta = RESPONSES_PROPIETARIO["rechaza_baja"].format(primer_nombre=primer_nombre)

    # ===================================================================
    # 4. MANTIENE PRECIO (OPCIÓN 2)
    # ===================================================================
    elif re.search(r'\b(2\s*(️⃣|$|\b)|dos|mantener|mantengo|dejo\s*igual|'
                   r'no\s+bajo|no\s+rebajo|no\s+ajusto|por\s*ahora\s*no|'
                   r'todav[íi]a\s*no|espero|prefiero\s*mantener|opci[óo]n\s*2)\b', texto, re.IGNORECASE):
        accion = "mantiene_precio"
        score = 5
        estado_campana = "mantiene_precio"
        respuesta = RESPONSES_PROPIETARIO["mantiene"].format(primer_nombre=primer_nombre)

    # ===================================================================
    # 5. PAUSA / YA SE VENDIÓ (OPCIÓN 3)
    # ===================================================================
    elif re.search(r'\b(3\s*(️⃣|$b)|tres|ya\s+se\s+(vend|arrend)|'
                   r'no\s+disponible|retir|pausa|sacame|borr[ao]|elimin[ao]|'
                   r'bajar\s*publicaci[óo]n|opci[óo]n\s*3)\b', texto, re.IGNORECASE):
        accion = "pausa_venta"
        score = 2
        estado_campana = "pausada_por_propietario"
        desactivar = True
        motivo_desactivacion = "pausa_voluntaria"
        respuesta = RESPONSES_PROPIETARIO["pausa"].format(primer_nombre=primer_nombre)

    # ===================================================================
    # 6. PREGUNTA POR FUENTE DE DATOS → ¡CALIENTE!
    # ===================================================================
    elif re.search(r'\b(d[oó]nde|fuente|sacaste|datos|cchc|cmf|informe|estad[íi]stica|verd[aá]d)\b', texto, re.IGNORECASE):
        accion = "pregunta_fuente"
        score = 10
        estado_campana = "caliente_pregunta_fuente"
        respuesta = f"¡Buena pregunta, {primer_nombre}!\n\nDatos oficiales noviembre 2025:\n" \
                    f"• CChC: 108.423 propiedades en stock\n" \
                    f"• CMF: créditos hipotecarios ↓38% anual\n" \
                    f"• Absorción RM: 32,4 meses (R.M.)\n\n" \
                    f"¿Quieres que te mande el PDF completo?\n\n" \
                    f"¿Seguimos con el ajuste del 7% para vender rápido? (1 = sí)"

    # ===================================================================
    # 7. FALLBACK: GROK AL RESCATE (cuando nada matchea)
    # ===================================================================
    else:
        try:
            from chatbot import call_grok
            prompt = f"""
Eres asistente senior de Procasa Jorge Pablo Caro Propiedades.
El propietario recibió una campaña proponiendo bajar 7% el precio 7% por el sobrestock.

Mensaje recibido:
"{original}"

Clasifica la intención real con UNA palabra y responde exactamente así:

BAJA → si acepta bajar (aunque diga "5%", "me parece mucho pero ok", etc.)
MANTIENE → si NO quiere tocar el precio
PAUSA → si ya vendió o quiere pausar
AGRESIVO → si está enojado o pide no contactar
PREGUNTA → si pregunta algo (cálculo, fuente, cuánto sería, etc.)
ESCALAR → si pide hablar con persona ("llámenme", "ejecutivo", etc.)

FORMATO OBLIGATORIO:
PALABRA||mensaje exacto para WhatsApp (máximo 380 caracteres, cálido, chileno)

Ejemplos:
BAJA||Perfecto don Luis, hacemos 6% si te sirve mejor, ya lo programo...
PREGUNTA||El 7% equivale a 280 UF menos, te paso el cálculo detallado...
ESCALAR||Claro doña María, Jorge Pablo te llama en 10 minutos.
"""

            grok_response = call_grok(prompt, temperature=0.0, max_tokens=200)

            if grok_response and "||" in grok_response:
                codigo_grok, _, mensaje_grok = grok_response.partition("||")
                codigo_grok = codigo_grok.strip().upper()

                if codigo_grok == "BAJA":
                    accion = "autoriza_baja_via_grok"
                    score = 10
                    estado_campana = "baja_autorizada_grok"
                    respuesta = mensaje_grok.strip()

                elif codigo_grok == "AGRESIVO":
                    desactivar = True
                    motivo_desactivacion = "rechazo_agresivo_grok"
                    respuesta = mensaje_grok.strip() or f"Disculpas {primer_nombre}, ya no recibirás más mensajes automáticos."

                elif codigo_grok in ["PREGUNTA", "ESCALAR"]:
                    accion = "caliente_via_grok"
                    score = 9
                    estado_campana = "caliente_grok"
                    respuesta = mensaje_grok.strip()

                else:
                    respuesta = mensaje_grok.strip()

            else:
                raise ValueError("Formato inválido")
        except Exception as e:
            print(f"[GROK FALLÓ] {e}")
            respuesta = RESPONSES_PROPIETARIO["default_caliente"].format(primer_nombre=primer_nombre, codigo=codigo)
            accion = "fallback_grok_error"
            score = 8

    # ===================================================================
    # GUARDADO EN MONGODB
    # ===================================================================
    update_data = {
        "clasificacion_propietario": accion,
        "ultima_respuesta": original,
        "fecha_clasificacion": datetime.now(timezone.utc),
        "autoriza_baja": "baja" in accion,
        "activo": not desactivar,
        "campanas.data_dura_7pct.estado": estado_campana,
        "campanas.data_dura_7pct.fecha_respuesta": datetime.now(timezone.utc),
        "campanas.data_dura_7pct.ultima_accion": accion,
        "campanas.data_dura_7pct.score": score,
    }
    if motivo_desactivacion:
        update_data["motivo_desactivacion"] = motivo_desactivacion

    contactos_collection.update_one(
        {"telefono": phone},
        {"$set": update_data,
         "$push": {"messages": {"$each": [
             {"role": "user", "content": original, "timestamp": datetime.now(timezone.utc)},
             {"role": "assistant", "content": respuesta, "timestamp": datetime.now(timezone.utc),
              "metadata": {"accion": accion, "score": score}}
         ]}}}
    )

    print(f"[DATA DURA 2025] {phone} → {accion.upper()} | score {score}")

    # ===================================================================
    # ENVÍO DE EMAIL SI ES CALIENTE (autoriza, pregunta o Grok lo marcó)
    # ===================================================================
    try:
        from email_utils import send_propietario_alert

        # Buscar todos los códigos del dueño
        cursor = contactos_collection.find({
            "$or": [
                {"telefono": phone},
                {"telefono": {"$regex": phone[-9:]}},
                {"propietario_telefono": {"$regex": phone[-9:]}}
            ],
            "codigo": {"$exists": True}
        })
        codigos = list(set([doc.get("codigo", "") for doc in cursor if doc.get("codigo")]))
        if not codigos:
            codigos = [codigo]

        if score >= 8 or "baja" in accion or accion.endswith("_grok"):
            send_propietario_alert(
                phone=phone,
                nombre=nombre_raw or "Propietario",
                codigos=codigos,
                mensaje_original=original,
                accion_detectada=accion,
                respuesta_bot=respuesta,
                autoriza_baja="baja" in accion
            )
    except Exception as e:
        print(f"[ERROR EMAIL PROPIETARIO] {e}")

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