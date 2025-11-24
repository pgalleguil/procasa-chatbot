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

def handle_propietario_respuesta(phone: str, user_msg: str, contacto: dict, contactos_collection) -> str:
    contacto = contacto or {}
    
    nombre_raw = contacto.get("nombre_propietario") or contacto.get("nombre") or "Propietario"
    primer_nombre = nombre_raw.strip().split(maxsplit=1)[0].title()
    codigo = contacto.get("codigo", "sin código")

    original = user_msg.strip()
    texto = original.lower()

# ===================================================================
    # 1. BAJA / AJUSTE / REBAJA (OPCIÓN 1)
    # ===================================================================
    if re.search(r'\b(1\s*(️⃣|$|\b)|uno|un[ao]?\b|'
                 # Verbos de acción directa
                 r'baj(?:ar|en|emos|arle|émoslo|ale|ita|bájen(?:la|lo|me)?)\b|'
                 r'rebaj(?:a|e|ar|émosla|ita|émoslo|en|émosle)\b|'
                 r'ajust(?:ar|emos|e|émoslo|émosle|ito|en)\b|'
                 r'modific(?:ar|a|an|alo|al[ao])\b|'  # NEW: Modificar
                 r'cambi(?:ar|emos|a|alo)\s*(?:el\s*)?(?:precio|valor)\b|' # NEW: Cambiar precio
                 # Sustantivos
                 r'baja|rebaja|ajuste|reducción|descuento|menor|menos|'
                 # Afirmaciones chilenas / Coloquiales
                 r's[ií]+|ok+|dale+|claro|obvio|ya\s*po|vamos|adelante|proced(?:a|e)|'
                 r'hag[áa]moslo|juegue|me\s*parece\s*bien|bueno|'
                 # Autorizaciones formales
                 r'autoriz|confirmo|acepto|apruebo|perfecto|listo|hecho|cuenta\s*conmigo|'
                 r'opci[óo]n\s*1\b|' # NEW: Opción 1 explícita
                 # Variaciones compuestas
                 r'(?:baj|rebaj|ajust)[ae]n|'
                 r'(?:5|6|7|8|9|10|12|15|20|25)\s*(%|por\s*ciento|puntos?)\b)', texto, re.IGNORECASE):

        ya_autorizo = contacto.get("autoriza_baja", False)
        accion = "autoriza_baja_automatica"
        score = 10
        estado_campana = "baja_autorizada"

        if ya_autorizo:
            respuesta = f"¡De nada {primer_nombre}! 😊\n\nYa tenemos todo listo para bajar el precio y venderla rápido.\nEn máximo 72 hrs verás los cambios publicados.\n¡Vamos con todo! 🔥"
        else:
            respuesta = RESPONSES_PROPIETARIO["autoriza_baja"].format(primer_nombre=primer_nombre)

# ===================================================================
    # 2. RECHAZO AGRESIVO (STOP / VETADO)
    # ===================================================================
    elif re.search(r'\b(no\s*molest|spam|denunci|bloqu|acoso|basta|para\s*ya|'
                   r'déjame\s*en\s*paz|cállate|no\s*contact|molestando|insist|'
                   r'qué\s*parte\s*de\s*no|déjame\s*tranquilo|'
                   # NEW: Específicos de base de datos / Chile
                   r'borr[ao]|elimin[ao]|sacame|sácame|borrar|eliminar|'
                   r'sernac|polic[íi]a|demand|' # Peligro legal
                   r'hincha|pesado|cortala|c[óo]rtala|' # Chilenismos de molestia
                   r'no\s*quiero\s*recibir|me\s*tienen\s*harto)\b', texto, re.IGNORECASE):
        respuesta = RESPONSES_PROPIETARIO["rechazo_agresivo"].format(primer_nombre=primer_nombre)
        accion = "rechazo_agresivo"
        score = 1
        estado_campana = "rechazo_agresivo"

    # ===================================================================
    # 3. RECHAZA LA BAJA (Vale más, no regalar)
    # ===================================================================
    elif re.search(r'\b(vale\s+m[áa]s|est[áa]s?\s*loco|rid[ií]culo|muy\s+bajo|'
                   r'no\s+acepto|no\s+estoy\s+de\s+acuerdo|tasaci[óo]n\s+errada|'
                   r'inaceptable|exagerado|negativo|'
                   # NEW: Conceptos de pérdida de valor
                   r'regal(?:ar|o)|botar|poca\s*plata|ni\s*cag|' # "Ni cagando" (común)
                   r'desvaloriz|pierd[oa]|perder|'
                   r'robo|estafa|muy\s*barat[oa])\b', texto, re.IGNORECASE):
        respuesta = RESPONSES_PROPIETARIO["rechaza_baja"].format(primer_nombre=primer_nombre)
        accion = "rechaza_baja_precio"
        score = 6
        estado_campana = "rechaza_baja"

    # ===================================================================
    # 4. MANTIENE PRECIO (OPCIÓN 2)
    # ===================================================================
    elif re.search(r'\b(2\s*(️⃣|$|\b)|dos|mantener|mantengo|dejo|queda|seguir\s*igual|'
                   r'por\s*ahora\s*no|todav[íi]a\s*no|espero|veamos|veo|prefiero\s*mantener|'
                   r'no\s+bajo|no\s+bajen|no\s+rebajo|no\s+ajusto|'
                   # NEW: Paciencia / Sin cambios
                   r'tal\s*cual|as[íi]\s*nom[áa]s|mismo\s*precio|mismo\s*valor|'
                   r'aguant|no\s*tengo\s*apuro|sin\s*apuro|no\s*tengo\s*prisa|'
                   r'opci[óo]n\s*2)\b', texto, re.IGNORECASE):
        respuesta = RESPONSES_PROPIETARIO["mantiene"].format(primer_nombre=primer_nombre)
        accion = "mantiene_precio"
        score = 5
        estado_campana = "mantiene_precio"

    # ===================================================================
    # 5. PAUSA / SACAR / NO DISPONIBLE (OPCIÓN 3)
    # ===================================================================
    elif re.search(r'\b(3\s*(️⃣|$|\b)|tres|pausa|retirar|quitar|sacar|no\s+disponible|'
                   r'ya\s*vend|se\s*vendi[óo]|arriend|no\s+vender|para\s*despu[ée]s|'
                   # NEW: Terminología de publicación
                   r'bajar\s*publicaci[óo]n|bajar\s*de\s*internet|'
                   r'suspend|congel|b[áa]jala\s*de|'
                   r'desist|no\s*sigan|'
                   r'opci[óo]n\s*3)\b', texto, re.IGNORECASE):
        respuesta = RESPONSES_PROPIETARIO["pausa"].format(primer_nombre=primer_nombre)
        accion = "pausa_venta"
        score = 2
        estado_campana = "pausa"

    # ===================================================================
    # 6. FALLBACK GROK (solo si nada coincidió)
    # ===================================================================
    else:
        try:
            from chatbot import call_grok
            prompt = f"""Mensaje del propietario: "{original}"

Clasifica con UNA sola palabra en mayúsculas:
BAJA / MANTIENE / PAUSA / AGRESIVO / RECHAZA / CALIENTE

Responde solo la palabra."""
            clasif = call_grok(prompt, temperature=0.0, max_tokens=10).strip().upper()

            if clasif == "BAJA":
                respuesta = RESPONSES_PROPIETARIO["autoriza_baja"].format(primer_nombre=primer_nombre)
                accion = "autoriza_baja_automatica"
                score = 10
                estado_campana = "baja_autorizada"
            elif clasif == "MANTIENE":
                respuesta = RESPONSES_PROPIETARIO["mantiene"].format(primer_nombre=primer_nombre)
                accion = "mantiene_precio"
                score = 5
                estado_campana = "mantiene_precio"
            elif clasif == "PAUSA":
                respuesta = RESPONSES_PROPIETARIO["pausa"].format(primer_nombre=primer_nombre)
                accion = "pausa_venta"
                score = 2
                estado_campana = "pausa"
            elif clasif == "AGRESIVO":
                respuesta = RESPONSES_PROPIETARIO["rechazo_agresivo"].format(primer_nombre=primer_nombre)
                accion = "rechazo_agresivo"
                score = 1
                estado_campana = "rechazo_agresivo"
            elif clasif == "RECHAZA":
                respuesta = RESPONSES_PROPIETARIO["rechaza_baja"].format(primer_nombre=primer_nombre)
                accion = "rechaza_baja_precio"
                score = 6
                estado_campana = "rechaza_baja"
            else:
                respuesta = RESPONSES_PROPIETARIO["default_caliente"].format(primer_nombre=primer_nombre, codigo=codigo)
                accion = "respuesta_caliente"
                score = 8
                estado_campana = "pendiente"
        except:
            respuesta = RESPONSES_PROPIETARIO["default_caliente"].format(primer_nombre=primer_nombre, codigo=codigo)
            accion = "fallback_error"
            score = 7
            estado_campana = "pendiente"

    # ===================================================================
    # GUARDADO UNIFICADO Y FINAL → TODO EN campanas.mercado_2025
    # ===================================================================
    desactivar = accion in ["pausa_venta", "rechazo_agresivo"]
    motivo = "pausa_voluntaria" if accion == "pausa_venta" else "rechazo_agresivo" if desactivar else None

    contactos_collection.update_one(
        {"telefono": phone},
        {"$set": {
            "clasificacion_propietario": accion,
            "ultima_respuesta": original,
            "fecha_clasificacion": datetime.now(timezone.utc),
            "autoriza_baja": accion == "autoriza_baja_automatica",
            "activo": not desactivar,
            "motivo_desactivacion": motivo,
            # ← UNIFICACIÓN TOTAL
            "campanas.mercado_2025.estado": estado_campana,
            "campanas.mercado_2025.fecha_respuesta": datetime.now(timezone.utc),
            "campanas.mercado_2025.ultima_accion": accion,
            "campanas.mercado_2025.score": score
        },
        "$push": {"messages": {"$each": [
            {"role": "user", "content": original, "timestamp": datetime.now(timezone.utc)},
            {"role": "assistant", "content": respuesta, "timestamp": datetime.now(timezone.utc),
             "metadata": {"accion": accion, "score": score}}
        ]}}}
    )

    print(f"[CAMPAÑA 2025] {phone} → {estado_campana.upper()} | {accion} (score {score})")

# ===================================================================
    # NUEVO 2025: DETECCIÓN INTELIGENTE DE MÚLTIPLES PROPIEDADES
    # ===================================================================
    try:
        tel_norm = "+" + re.sub(r"\D", "", phone)[-11:]

        cursor = contactos_collection.find({
            "$or": [
                {"telefono": {"$regex": tel_norm[-9:]}},
                {"propietario_telefono": {"$regex": tel_norm[-9:]}},
                {"telefono": tel_norm}
            ],
            "tipo": "propiedad"
        })
        todas_propiedades = list(cursor)

        if len(todas_propiedades) > 1:
            def detectar_en_texto(texto):
                matches = []
                for prop in todas_propiedades:
                    score = 0
                    campos = [
                        str(prop.get("comuna", "")).lower(),
                        str(prop.get("direccion", "")).lower(),
                        str(prop.get("proyecto", "")).lower(),
                        str(prop.get("nombre_edificio", "")).lower(),
                        str(prop.get("codigo", "")).lower(),
                        f"{prop.get('dormitorios','')}d".lower(),
                        f"{prop.get('dormitorios','')} dorm".lower(),
                    ]
                    for campo in campos:
                        if campo and campo in texto: score += 30
                        for palabra in campo.split():
                            if len(palabra) > 3 and palabra in texto: score += 8
                    if score > 15:
                        matches.append({"prop": prop, "score": score})
                matches.sort(key=lambda x: x["score"], reverse=True)
                return matches[:5]

            props_detectadas = detectar_en_texto(texto)

            if props_detectadas and ("autoriza_baja" in accion or "pausa" in accion):
                lista = "\n".join([
                    f"• {p['prop'].get('comuna','?').title()} - {p['prop'].get('direccion','sin dirección')[:50]}"
                    for p in props_detectadas
                ])

                if "autoriza_baja" in accion:
                    respuesta = f"¡Perfecto {primer_nombre}! Entendí clarito:\n\n{lista}\n\nYa programé el ajuste de precio en esas propiedades específicas.\nEn máximo 72 hrs verás los nuevos valores publicados.\n¡Vamos con todo!"
                else:  # pausa
                    respuesta = f"Recibido {primer_nombre}, entendí:\n\n{lista}\n\nYa dejé esas propiedades en pausa. No recibirás más notificaciones de ellas.\nCuando quieras reactivar, solo escribe 'Reactivar'."

                for match in props_detectadas:
                    prop_id = match["prop"].get("_id")
                    if prop_id:
                        contactos_collection.update_one(
                            {"_id": prop_id},
                            {"$set": {
                                "campanas.mercado_2025.estado": "ajuste_programado" if "autoriza_baja" in accion else "pausada_por_propietario",
                                "campanas.mercado_2025.fecha_ultima_interaccion": datetime.now(timezone.utc)
                            }}
                        )

                contactos_collection.update_one(
                    {"telefono": phone},
                    {"$set": {
                        "campanas.mercado_2025.propiedades_detectadas": len(props_detectadas)
                    }}
                )

                print(f"[MULTI-PROPIEDAD] Detectadas {len(props_detectadas)} propiedades específicas")

    except Exception as e:
        print(f"[ERROR MULTI-PROPIEDAD] {e}")

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