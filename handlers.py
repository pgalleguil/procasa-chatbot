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
    texto_lower = original.lower().strip()

    nombre_raw = contacto.get("nombre_propietario") or contacto.get("nombre") or "Cliente"
    primer_nombre = nombre_raw.split(maxsplit=1)[0].title()

    # === MODO PRUEBA PARA TI (Jorge) → usa propiedad real ===
    if phone == "+56983219804":
        codigos = ["55268"]  # Casa real: 16.900 UF, Santiago Centro
    else:
        codigos = [contacto.get("codigo")] if contacto.get("codigo") else []
        if not codigos:
            codigos = [doc.get("codigo") for doc in contactos_collection.find({"telefono": phone}, {"codigo": 1}) if doc.get("codigo")]

    # === CARGAR DATOS REALES DE universo_obelix ===
    datos_propiedades = {}
    for cod in codigos:
        if cod:
            info = cargar_datos_propiedad(cod)
            if info and not info.get("error"):
                datos_propiedades[cod] = info

    if not datos_propiedades:
        return "Entiendo, disculpa si no fui clara. ¿Me confirmas el código de tu propiedad para continuar?"

    # === PROPIEDAD PRINCIPAL ===
    cod, info = list(datos_propiedades.items())[0]
    precio_uf = info.get("precio_uf_actual")
    precio_nuevo = round(precio_uf * 0.93, 1) if precio_uf else None
    comuna = info.get("comuna", "Santiago")
    tipo = info.get("tipo", "Propiedad")

    # === ESTADO DE CONVERSACIÓN (asumiendo template ya enviado) ===
    messages = contacto.get("messages", [])
    historial = [m for m in messages[-10:] if m.get("role") == "assistant"]
    ya_mostro_link = any("procasa.cl" in m.get("content", "") for m in historial)
    autorizo_baja = contacto.get("autoriza_baja", False)

    # === ANTI-SPAM EMAIL ===
    campana = contacto.get("campanas", {}).get("data_dura_7pct", {})
    ya_envio_email = campana.get("email_enviado", False)

    debe_enviar_email = False
    if not ya_envio_email:
        if autorizo_baja or contacto.get("clasificacion_propietario", "").startswith("autoriza"):
            debe_enviar_email = True
        elif any(palabra in texto_lower for palabra in ["precio", "cuanto", "quedaría", "visitas", "ofertas", "ajuste", "bajar", "confirmar", "segura", "rápido", "contactar", "llamar"]):
            debe_enviar_email = True  # Incluye "contactar/llamar" para escalados
        elif len(messages) <= 6:
            debe_enviar_email = True

    if debe_enviar_email:
        contactos_collection.update_one(
            {"telefono": phone},
            {"$set": {"campanas.data_dura_7pct.email_enviado": True}}
        )

    # === REGEX PARA DETECCIÓN RÁPIDA ===
    ACEPTA = re.compile(r'\b(1|opci[oó]n\s*1|uno|s[íií]+|ok+|dale+|adelante|perfecto|confirm[ao]|autoriz[ao]|listo)\b', re.I)
    PREGUNTA_PRECIO = re.compile(r'\b(cu[áa]nto|precio|quedar[íi]a|final|uf|valor)\b', re.I)
    PREGUNTA_VISITAS = re.compile(r'\b(visitas|interes|ofertas|movimiento|gente|verla|segura|rápido|cerrar|vender|tiempo)\b', re.I)
    PREGUNTA_PROPIEDAD = re.compile(r'\b(cu[áa]l|qué|de qué|de cual|código|propiedad)\b', re.I)
    PAUSA = re.compile(r'\b(3|opci[oó]n\s*3|tres|ya\s*(vend|arriend)|retirar|pausa|sacar|no\s*disponible)\b', re.I)  # Para "no disponible" – bajar del sistema

    respuesta = ""
    accion = "continua_con_grok"
    score = 8

    # 1. ACEPTA LA BAJA
    if not autorizo_baja and ACEPTA.search(texto_lower):
        respuesta = RESPONSES_PROPIETARIO["autoriza_baja"].format(primer_nombre=primer_nombre)
        accion = "autoriza_baja_automatica"
        score = 10
        contactos_collection.update_one({"telefono": phone}, {"$set": {"autoriza_baja": True}})

    # 2. PAUSA / NO DISPONIBLE → Bajar del sistema (sin email)
    elif PAUSA.search(texto_lower):
        respuesta = RESPONSES_PROPIETARIO["pausa"].format(primer_nombre=primer_nombre)
        accion = "pausa_venta"
        score = 2
        contactos_collection.update_one({"telefono": phone}, {"$set": {"activo": False, "disponible": False, "fecha_pausa": datetime.now(timezone.utc)}})  # Bajar propiedad

    # 3. PREGUNTA POR PRECIO
    elif PREGUNTA_PRECIO.search(texto_lower):
        respuesta = f"Entiendo, {primer_nombre}. Siguiendo el mensaje de hace unos minutos, el precio quedaría en **{precio_nuevo:,.1f} UF**.\n\n" \
                    f"Ese valor nos posiciona perfecto en el rango que los bancos están financiando hoy (1.800-1.900 créditos/mes, tasas ~4.42%).\n\n" \
                    f"¿Me das luz verde para bajarla a {precio_nuevo:,.1f} UF y empezar a recibir ofertas serias esta semana?"

    # 4. PREGUNTA POR VISITAS / VELOCIDAD (datos reales)
    elif PREGUNTA_VISITAS.search(texto_lower):
        respuesta = f"Entiendo perfectamente tu preocupación, {primer_nombre} — es normal en este mercado estancado (CChC: absorción 30-32 meses).\n\n" \
                    f"Sí, el ajuste acelera: propiedades similares ven ventas +5-10% (Colliers noviembre 2025), cerrando en 90-120 días vs. 18-24 meses promedio.\n\n" \
                    f"Con {precio_nuevo:,.1f} UF tu {tipo.lower()} entra al rango viable, antes de que el cambio político incremente la oferta +10-20% (El Mercurio).\n\n" \
                    f"¿Me autorizas el 7% para reactivarla esta semana?"

    # 5. PREGUNTA “¿CUÁL PROPIEDAD?”
    elif PREGUNTA_PROPIEDAD.search(texto_lower):
        link = f"https://www.procasa.cl/{cod}"
        respuesta = f"Es tu {tipo.lower()} en {comuna}, código {cod} — la que te mencionamos en el mensaje inicial.\n\n" \
                    f"Actualmente en {precio_uf:,.0f} UF → con el 7% quedaría en **{precio_nuevo:,.1f} UF**.\n\n" \
                    f"Link directo: {link}\n\n" \
                    f"¿Me das luz verde para el ajuste y empecemos a moverla?"

    # 6. CUALQUIER OTRA RESPUESTA → GROK FLUIDO (prompt corto, enfocado en no repetir)
    else:
        prompt = f"""
Eres asistente del área de Inteligencia de Negocios de Procasa Jorge Pablo Caro Propiedades.
El cliente ya recibió el template inicial (mensaje con datos mercado y opciones 1-3). Continúa la conversación de forma natural, como respuesta directa al mensaje anterior.

Propiedad: {tipo} en {comuna}, código {cod}
Precio actual: {precio_uf:,.0f} UF
Precio con -7%: {precio_nuevo:,.1f} UF
Link: https://www.procasa.cl/{cod} (solo si pregunta por la propiedad)

Datos reales mercado Chile 25 noviembre 2025 (usa para convencer con honestidad, empatía y urgencia agresiva – cita fuentes breves, motiva con acciones inmediatas post-ajuste):
- CChC Q3 2025: Stock RM ~108.000 propiedades (histórico alto), absorción 30-32 meses, ventas estancadas (-13% 2024, +5-10% proyectado 2025 por tasas bajas).
- Cesantía RM 8.0-8.5% (INE noviembre): Reduce demanda -10-15% (posterga compras, impacto créditos medios).
- Incremento UF +3.5-4% anual (Banco Central IPoM noviembre): Presiona precios reales -2-5% CLP en vivienda (inflación ~3.5%).
- Créditos hipotecarios ~1.800-1.900/mes (CChC/Banco Central), tasas 4.42% (mínimo anual noviembre, DF) – endurecimiento por cesantía.
- Efecto ajuste 5-7%: Ventas +5-10% (Colliers noviembre), cierres 90-120 días vs. 18-24 meses promedio (ajustes obligatorios por sobreoferta 105k-108k, GlobalBrokers).
- Nuevo gobierno derecha (eventual escenario presidencial de Kast): Políticas pro-mercado (incentivos inversión), pero prepara +10-20% propiedades disponibles (más oferta por confianza, El Mercurio/CNN).
- Escenario cambio gobierno: Volatilidad tasas (baja a 4-4.5% si estabilidad), riesgo estancamiento ventas si cesantía >8%; recuperación gradual (+5-10% ventas 2025, foco regionales Valparaíso/Biobío +45% hipotecas, Colliers).
- Tendencia: Precios a la baja -1-3% (premium Vitacura/Las Condes); inversión -8.3% 2024 a +2-5% 2025 (CChC); sostenibilidad (MINVU Agenda Urbana 2025, +30% proyectos certificados).
- Acciones inmediatas post-ajuste (motiva confirmación agresiva): Actualizamos precio en portales YA (24-48 hrs), destacamos propiedad en top búsquedas (+20-30% visibilidad, Colliers), lanzamos campaña agresiva notificando de inmediato a TODOS los clientes/leads que se interesaron en la propiedad ("¡Rebaja 7%! Nuevo precio {precio_nuevo} UF – ¡Visita ya!"), y enviamos blast a 50+ leads/prospectos interesados en propiedades similares en {comuna}/tipo/precio para + visitas en 72 hrs; reactivamos marketing total (anuncios targeted, emails calientes, reactivación leads dormidos).

Historial reciente (responde basado en esto, sin repetir):
{chr(10).join([f"{m.get('role','?')}: {m.get('content','')[:100]}..." for m in messages[-4:]])}

Mensaje del cliente: {original}

Reglas estrictas (PRIORIDAD 1):
- NO digas "Hola", NO te presentes, NO repitas datos del template inicial.
- Valida duda con empatía: "Entiendo tu preocupación, es normal en este mercado lento".
- Fluye natural: Responde directo al mensaje anterior, usa historial para no repetir.
- Convence con 1-2 datos reales (cita breve: "según CChC noviembre").
- Máximo 4-5 líneas, tono cálido-profesional.
- Termina SIEMPRE invitando a confirmar: "¿Me das luz verde para el ajuste?".

Responde SOLO el texto natural para WhatsApp.
"""

        from chatbot import call_grok
        respuesta_grok = call_grok(prompt, temperature=0.3, max_tokens=500)  # Temp baja para menos "creatividad"
        if not respuesta_grok or "Hola" in respuesta_grok or len(respuesta_grok) < 50:  # Fallback si falla flujo
            respuesta = f"Entiendo tu punto, {primer_nombre}. Siguiendo lo que te contábamos hace unos minutos, con {precio_nuevo:,.1f} UF entramos al rango viable (CChC: absorción 30 meses). ¿Me das luz verde para reactivarla y empecemos a recibir ofertas?"
        else:
            respuesta = respuesta_grok.strip()

    # === GUARDAR EN MONGO ===
    update_data = {
        "campanas.data_dura_7pct.ultima_accion": accion,
        "campanas.data_dura_7pct.score": score,
        "campanas.data_dura_7pct.fecha_respuesta": datetime.now(timezone.utc),
        "clasificacion_propietario": accion
    }
    contactos_collection.update_one(
        {"telefono": phone},
        {"$set": update_data,
         "$push": {"messages": {"$each": [
             {"role": "user", "content": original},
             {"role": "assistant", "content": respuesta, "metadata": {"accion": accion, "score": score}}
         ]}}}
    )

    # === EMAIL → SOLO 1 VEZ Y CUANDO VALE LA PENA (incluye "contactar/llamar") ===
    if debe_enviar_email:
        try:
            from email_utils import send_propietario_alert
            send_propietario_alert(
                phone=phone,
                nombre=nombre_raw,
                codigos=codigos or ["sin código"],
                mensaje_original=original,
                accion_detectada=accion,
                respuesta_bot=respuesta,
                autoriza_baja=(accion == "autoriza_baja_automatica" or autorizo_baja)
            )
            print(f"[EMAIL ENVIADO] {phone} → {accion}")
        except Exception as e:
            print(f"[ERROR EMAIL] {e}")
    else:
        print(f"[EMAIL NO ENVIADO] {phone} → ya enviado o no calienta")

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