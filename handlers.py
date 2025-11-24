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
    original = user_msg.strip()
    texto = original.lower().strip()

    nombre_raw = contacto.get("nombre_propietario") or contacto.get("nombre") or "Propietario"
    primer_nombre = nombre_raw.strip().split(maxsplit=1)[0].title()
    codigo = contacto.get("codigo", "sin código")

    # === HISTORIAL RECIENTE (para que Grok no se olvide) ===
    messages = contacto.get("messages", []) if contacto else []
    ultimo_bot = next((m["content"] for m in reversed(messages) if m.get("role") == "assistant"), "")
    ya_autorizo = any("excelente decisión" in m.get("content", "").lower() for m in messages if m.get("role") == "assistant")
    ultima_accion = contacto.get("campanas", {}).get("data_dura_7pct", {}).get("ultima_accion", "")

    respuesta = ""
    accion = "sin_clasificar"
    score = 5
    estado_campana = "pendiente"
    desactivar = False
    motivo_desactivacion = None

    # ===================================================================
    # 1. RECHAZO AGRESIVO → PRIORIDAD MÁXIMA (vetar)
    # ===================================================================
    if re.search(r'\b(no\s*molest|spam|denunci|bloqu|acoso|basta|para\s*ya|'
                 r'd[ée]jame\s*en\s*paz|c[áa]llate|no\s*contact|molestando|insist|'
                 r'borr[ao]|elimin[ao]|sacame|s[áa]came|sernac|polic[íi]a|demand|'
                 r'qué\s*parte\s*de\s*no|hincha|pesado|cortala|no\s*escribas\s*m[áa]s)\b', texto, re.IGNORECASE):
        accion = "rechazo_agresivo"
        score = 1
        estado_campana = "vetado"
        desactivar = True
        motivo_desactivacion = "rechazo_agresivo"
        respuesta = f"Lamento mucho si fue inoportuno, {primer_nombre}. Ya eliminé tu número de todas las campañas. No recibirás más mensajes."

    # ===================================================================
    # 2. YA VENDIÓ / PAUSA / SACAR PUBLICACIÓN
    # ===================================================================
    elif re.search(r'\b(ya\s+se\s+(vend|arrend)|retir|pausa|sacame|borr[ao]|elimin[ao]|'
                   r'bajar\s*publicaci[óo]n|no\s+disponible|opci[óo]n\s*3)\b', texto, re.IGNORECASE):
        accion = "pausa_venta"
        score = 2
        estado_campana = "pausada_por_propietario"
        desactivar = True
        motivo_desactivacion = "pausa_voluntaria"
        respuesta = RESPONSES_PROPIETARIO["pausa"].format(primer_nombre=primer_nombre)

    # ===================================================================
    # 3. ACEPTA BAJA CLARO (solo la primera vez)
    # ===================================================================
    elif not ya_autorizo and re.search(r'\b(1\b|uno\b|s[ií]+\b|ok+\b|dale+\b|claro\b|ya\s*po\b|vamos\b|'
                                       r'adelante\b|proced(?:a|e)\b|hag[áa]moslo\b|perfecto\b|listo\b|hecho\b|'
                                       r'autoriz[ao]\b|confirm[ao]\b|acept[ao]\b|aprueb[ao]\b|opci[óo]n\s*1)\b', texto, re.IGNORECASE):
        accion = "autoriza_baja_automatica"
        score = 10
        estado_campana = "baja_autorizada"
        respuesta = f"¡Excelente decisión, {primer_nombre}!\n\n" \
                    f"Ya programé el ajuste del precio para que tu propiedad entre en el rango de los pocos créditos que están aprobando hoy.\n" \
                    f"En máximo 72 horas verás el nuevo valor publicado en todos los portales.\n\n" \
                    f"¡Vamos con todo a cerrar esta venta rápido! 🔥"

    # ===================================================================
    # 4. NEGOCIACIÓN DE % (después de aceptar o en cualquier momento)
    # ===================================================================
    elif re.search(r'\b(\d+\s*%|\d+\s*puntos?|solo\s*\d+%|puedo\s*\d+%|m[áa]ximo\s*\d+%|'
                   r'\d+%\s*(mejor|est[áa] bien)|me\s*parece\s*mucho|much[io]simo|exagerado)\b', texto, re.IGNORECASE):
        accion = "negociacion_porcentaje"
        score = 10
        estado_campana = "baja_negociada"
        respuesta = f"¡Entendido perfectamente, {primer_nombre}!\n\n" \
                    f"Estamos 100% alineados en vender rápido. Un ajuste más suave también nos ayuda muchísimo.\n" \
                    f"¿Te sirve {primer_nombre} un 4,5% o 5%? Así entramos al rango top de créditos aprobados esta semana.\n\n" \
                    f"¡Dime y lo programamos hoy mismo!"

    # ===================================================================
    # 5. PREGUNTA POR FUENTE / DATOS / CCHC / ESTADÍSTICAS
    # ===================================================================
    elif re.search(r'\b(d[oó]nde|fuente|sacaste|datos|cchc|cmf|informe|estad[íi]stica|'
                   r'cu[áa]nto\s*ser[íi]an|cu[áa]nto\s*uf|verd[aá]d|real)\b', texto, re.IGNORECASE):
        accion = "pregunta_fuente_o_uf"
        score = 10
        estado_campana = "caliente_pregunta"
        respuesta = f"¡Buena pregunta, {primer_nombre}!\n\n" \
                    f"Datos oficiales noviembre 2025:\n" \
                    f"• CChC: 108.423 propiedades en stock\n" \
                    f"• CMF: créditos hipotecarios ↓38% anual\n" \
                    f"• Absorción RM: 32,4 meses\n\n" \
                    f"¿Te mando el PDF completo?\n" \
                    f"¿O seguimos con el ajuste (aunque sea 5%) para vender antes de fin de año?"

    # ===================================================================
    # 6. RECHAZA LA BAJA (pero no está enojado)
    # ===================================================================
    elif re.search(r'\b(no\s+acepto|inaceptable|muy\s+bajo|rid[ií]culo|exagerado|'
                   r'no\s+bajo|no\s+rebajo|mantengo|opci[óo]n\s*2)\b', texto, re.IGNORECASE):
        accion = "rechaza_baja_precio"
        score = 6
        estado_campana = "rechaza_baja"
        respuesta = RESPONSES_PROPIETARIO["rechaza_baja"].format(primer_nombre=primer_nombre)

    # ===================================================================
    # 7. FALLBACK INTELIGENTE CON GROK (usa todo el contexto)
    # ===================================================================
    else:
        try:
            from chatbot import call_grok

            # Resumen del historial para Grok
            contexto = ""
            for m in messages[-8:]:
                rol = "Propietario" if m.get("role") == "user" else "Bot"
                contenido = m.get("content", "")[:100]
                contexto += f"{rol}: {contenido}\n"

            prompt = f"""
Eres una asistete de Jorge Pablo Caro, corredor senior de Procasa. Estás hablando con {primer_nombre}, propietario código {codigo}.

Contexto completo del chat:
{contexto}

Último mensaje del propietario:
"{original}"

Ya propusiste bajar 7% por sobrestock. El propietario ya dijo "sí" o está negociando.

Clasifica y responde EXACTAMENTE así:
INTENCIÓN||Respuesta natural cálida chilena (máx 380 caracteres)

Posibles intenciones:
- ACEPTA_MENOS (acepta 3-6%)
- PIDE_LLAMADA (quiere hablar con persona)
- PREGUNTA_UF (cuánto sería en plata)
- DUDOSO (pide tiempo o más info)
- MANTIENE (no quiere bajar nada)

Ejemplos:
ACEPTA_MENOS||Perfecto {primer_nombre}, 5% está genial! Así vendemos antes de navidad. ¿Lo programamos?
PIDE_LLAMADA||Claro {primer_nombre}, te llamo en 5 minutos para cerrar el ajuste juntos
PREGUNTA_UF||Serían 245 UF menos aproximadamente. ¿Te paso el cálculo exacto por mail?
"""

            grok_out = call_grok(prompt, temperature=0.15, max_tokens=300)
            if grok_out and "||" in grok_out:
                intencion, _, resp = grok_out.partition("||")
                intencion = intencion.strip().upper()

                if "ACEPTA" in intencion:
                    accion = "baja_aceptada_grok"
                    score = 10
                    estado_campana = "baja_autorizada_grok"
                elif "PIDE_LLAMADA" in intencion:
                    accion = "escalado_llamada"
                    score = 10
                elif "PREGUNTA_UF" in intencion:
                    accion = "pregunta_calculo"
                    score = 9
                else:
                    accion = "continua_con_grok"
                    score = 8

                respuesta = resp.strip()
            else:
                raise ValueError("Sin ||")
        except Exception as e:
            print(f"[GROK FALLÓ PROPIETARIO] {e}")
            respuesta = f"Entendido {primer_nombre}, gracias por responder. ¿En qué te puedo ayudar exactamente con el precio? ¿Prefieres que te llame?"

    # ===================================================================
    # GUARDADO FINAL EN MONGO + EMAIL SI ES CALIENTE
    # ===================================================================
    update_data = {
        "clasificacion_propietario": accion,
        "ultima_respuesta": original,
        "fecha_clasificacion": datetime.now(timezone.utc),
        "autoriza_baja": "baja" in accion or "acepta" in accion,
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

    # Email solo si es caliente
    if score >= 8 or "baja" in accion or "acepta" in accion:
        try:
            from email_utils import send_propietario_alert
            codigos = [doc.get("codigo") for doc in contactos_collection.find(
                {"$or": [{"telefono": phone}, {"telefono": {"$regex": phone[-9:]}}]}, {"codigo": 1}
            ) if doc.get("codigo")]
            send_propietario_alert(
                phone=phone, nombre=nombre_raw, codigos=codigos or [codigo],
                mensaje_original=original, accion_detectada=accion,
                respuesta_bot=respuesta, autoriza_baja="baja" in accion or "acepta" in accion
            )
        except Exception as e:
            print(f"[ERROR EMAIL PROP] {e}")

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