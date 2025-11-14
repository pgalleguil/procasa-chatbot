import os
from datetime import datetime, timezone
from config import Config
from db import load_chat_history, save_chat_history, get_recommended_codes, update_recommended_codes, CHATS_COLLECTION, load_current_criteria
from rag import search_properties
from ai_utils import analyze_sentiment, classify_intent, classify_urgency, extract_criteria, generate_response, is_feedback_mode
from ai_utils import clean_for_json

config = Config()

# NUEVO: Template para inicializar contexto de feedback (outreach post-interés/visita)
MENSAJE_TEMPLATE = """
Hola, soy asistente inmobiliaria de PROCASA Jorge Pablo Caro Propiedades. 😊 

Recordamos que hace poco mostraste interés en una de nuestras propiedades y contactaste a uno de nuestros ejecutivos. ¿Pudiste coordinar y visitar alguna opción que te gustara? ¿Qué te pareció la experiencia?

Si sigues en la búsqueda de tu hogar ideal, me encantaría saber qué estás priorizando ahora: ¿dormitorios, comuna, presupuesto? ¡Estoy aquí para mostrarte opciones que se ajusten perfecto a lo que buscas!

Cuéntame un poco más para reconectarte con lo mejor de nuestra cartera.
"""

def initialize_feedback_history(phone: str) -> list:
    """
    Inicializa historial para leads de feedback post-outreach.
    Agrega el template como mensaje inicial de assistant.
    """
    template_msg = {"role": "assistant", "content": MENSAJE_TEMPLATE.strip(), "timestamp": datetime.now(timezone.utc)}
    initial_history = [template_msg]
    print(f"[LOG] Historial inicializado con feedback template para {phone}.")
    return initial_history

def process_message(phone: str, message: str, history: list = None, active: bool = True, extras: dict = None) -> bool:
    # FIX3: Chequea active DESDE DB ANTES de todo (fresh fetch)
    doc = CHATS_COLLECTION.find_one({"phone": phone})
    active = doc.get("active", True) if doc else True
    do_not_contact = doc.get("do_not_contact", False) if doc else False
    # NUEVO: Carga alert_email_sent aquí para reutilizar en refinamientos (integra fix de escalada)
    alert_email_sent = doc.get("alert_email_sent", False) if doc else False

    if not active or do_not_contact:
        if do_not_contact:
            print(f"[LOG] Do_not_contact activo para {phone}, ignorando mensaje.")
        else:
            print(f"[LOG] Chat inactivo para {phone}, ignorando mensaje.")
            # FIX: Para reactivar si user vuelve (e.g., corrige typo), chequea si intent urgente
            intent = classify_intent(history or [], message)  # History=None ok
            if intent in ["AGENDAR_VISITA", "SOLICITUD_CONTACTO", "VENDER_MI_CASA"]:
                CHATS_COLLECTION.update_one({"phone": phone}, {"$set": {"active": True}})
                active = True
                print(f"[LOG] Reactivado por intent urgente: {intent}.")
            else:
                return True  # Ignora
    
    if history is None:
        history = []
    
    # Detecta si es primer mensaje ANTES de append
    is_first_message = len([msg for msg in history if msg["role"] == "user"]) == 0
    
    # NUEVO: Detecta feedback_mode para boost y suavizado
    feedback_mode = is_feedback_mode(history)
    print(f"[DEBUG] Feedback_mode detectado: {feedback_mode}")  # ← TEMPORAL para log
    
    # FIX: Intent PRIMERO (para outreach); sentiment después (solo si no prioritario)
    intent = classify_intent(history, message)
    
    # NUEVO: Boostea intent para greetings en feedback_mode (mueve lógica aquí para compatibilidad)
    if feedback_mode:
        greeting_keywords = ["hola", "buenas", "hey", "saludos", "puedes ser", "alo", "hola"]
        if intent == "OTRO" and any(kw in message.lower() for kw in greeting_keywords):
            intent = "FEEDBACK_VISITA" if any(word in message.lower() for word in ["visita", "conocerla", "vi", "fue"]) else "BUSCAR_MAS"
            print(f"[LOG] Intent boosteado a '{intent}' por greeting en feedback_mode.")
    
    # NUEVO (ANÁLISIS): Manejo especial para media/reacciones
    if message.startswith("[MEDIA]") or intent == "OTRO":
        if history and history[-1]["role"] == "assistant" and "propiedad" in history[-1]["content"].lower():
            response = "¡Me alegra tu reacción! 😊 ¿Te gusta esa opción? ¿Quieres más detalles o similares en esa zona?"
            ai_msg = {"role": "assistant", "content": response, "timestamp": datetime.now(timezone.utc)}
            history.append(ai_msg)
            save_chat_history(phone, history)
            print(f"[BOT -> {phone}] {response} (contextual por media)")
            return True
        # Sino, fallback normal
    
    # NUEVO: Maneja RECHAZO/EN_PROCESO para outreach PRIMERO
    if intent == "RECHAZO":
        response = "Entendido, gracias por tu tiempo. No te contactaremos más. ¡Que tengas un buen día! 👍"
        # Marca flag en DB
        CHATS_COLLECTION.update_one({"phone": phone}, {"$set": {"do_not_contact": True, "active": False}})
        ai_msg = {"role": "assistant", "content": response, "timestamp": datetime.now(timezone.utc)}
        history.append(ai_msg)
        save_chat_history(phone, history)
        print(f"[BOT -> {phone}] {response}")
        print(f"[LOG] RECHAZO procesado: do_not_contact=True para {phone}.")
        return False
    elif intent == "EN_PROCESO":
        response = "Perfecto, te derivamos a tu ejecutivo de inmediato. Te contactará en breve. ¡Gracias! 👍"
        criteria = {"escalar_humano": True}  # Fuerza escala
        ai_msg = {"role": "assistant", "content": response, "timestamp": datetime.now(timezone.utc), "criteria": criteria}
        history.append(ai_msg)
        save_chat_history(phone, history)
        print(f"[BOT -> {phone}] {response}")
        print(f"[ALERTA] EN_PROCESO: Escalar humano para {phone}.")
        return True
    
    # FIX: Maneja SOLICITUD_CONTACTO temprano para escalada inmediata
    if intent == "SOLICITUD_CONTACTO":
        response = "¡Por supuesto! Te conecto inmediatamente con uno de nuestros asesores expertos de Procasa. Te contactarán en breve para ayudarte personalmente. ¿Me das tu nombre para pasarles el detalle? 😊"
        criteria = {"escalar_humano": True, "necesita_mas_info": False}  # Fuerza escala y cierra loop
        ai_msg = {"role": "assistant", "content": response, "timestamp": datetime.now(timezone.utc), "criteria": criteria}
        history.append(ai_msg)
        save_chat_history(phone, history)
        print(f"[BOT -> {phone}] {response}")
        print(f"[ALERTA URGENTE] SOLICITUD_CONTACTO: Escalar humano para {phone}.")
        return True
   
    # Sentiment DESPUÉS (solo si no prioritario)
    sentiment = analyze_sentiment(message)
    if sentiment == "NEGATIVE" and intent not in ["RECHAZO", "EN_PROCESO", "SOLICITUD_CONTACTO"]:
        # NUEVO: Extrae criteria para preservar (incluso en negative, por si corrige)
        criteria = extract_criteria(history, message, phone)
        end_response = "Entiendo, gracias por tu tiempo. Si cambias de opinión, estamos aquí para ayudarte."
        ai_msg = {
            "role": "assistant", 
            "content": end_response, 
            "timestamp": datetime.now(timezone.utc),
            "criteria": criteria  # ← FIX: Preserva criteria en ai_msg
        }
        history.append(ai_msg)
        save_chat_history(phone, history)
        # FIX: Usa find_one_and_update atómico para active
        CHATS_COLLECTION.find_one_and_update(
            {"phone": phone},
            {"$set": {"active": False, "last_updated": datetime.now(timezone.utc)}}
        )
        print(f"[LOG] Chat marcado inactivo en DB para {phone} (atómico).")
        print(f"[BOT] {end_response}")
        print(f"[LOG] Chat terminado por NEGATIVE para {phone}.")
        return False
    
    urgency = classify_urgency(history, message)
    
    # NUEVO: Load current_criteria de DB y merge con extract
    db_criteria = load_current_criteria(phone)
    extracted = extract_criteria(history, message, phone)  # Pasa phone para merge en extract
    criteria = {**db_criteria, **extracted}  # Merge: prioriza extracted (nuevo msg)
    # Limpia criteria para logs (evita serialización issues en prints)
    clean_criteria = clean_for_json(criteria)
    print(f"[LOG] Criteria merged (DB + extract): {clean_criteria}")
    
    # FIX: Normaliza prefs_semánticas a string para checks (evita error si lista vacía)
    prefs_sem = criteria.get('prefs_semánticas', '')
    if isinstance(prefs_sem, list):
        prefs_sem = ' '.join(prefs_sem) if prefs_sem else ''
    prefs_lower = prefs_sem.lower()
    print(f"[DEBUG] prefs_semánticas normalizada: '{prefs_lower}'")
    
    # NUEVO: Reset si indica nueva búsqueda (ampliado keywords, full clean en DB al guardar)
    reset_keywords = ["otra propiedad", "nueva búsqueda", "busco otra", "cambia todo", "empieza de nuevo", "esta es otra", "diferente propiedad", "busco separado"]
    do_full_reset = intent == "OTRO" and any(kw in message.lower() for kw in reset_keywords)
    if do_full_reset:
        # Limpia cores y features, mantiene solo prefs_semánticas si aplica
        criteria = {k: v for k, v in criteria.items() if k in ['prefs_semánticas']}
        print(f"[LOG] Reset criteria por nueva búsqueda: {criteria}")
        # Al guardar, fuerza current_criteria={}; también limpia recommended_codes si full
        CHATS_COLLECTION.update_one({"phone": phone}, {"$set": {"current_criteria": {}, "recommended_codes": []}})
    
    # FIX: Si 'financiamiento' in prefs → escalar_humano=True (tu regla) – usa prefs_lower
    if 'financiamiento' in prefs_lower:
        criteria['escalar_humano'] = True
        print(f"[ALERTA] Financiamiento detectado: Escalar humano.")
    
    # NUEVO (ANÁLISIS): Contador refinamientos para boost escalada
    recent_intents = [msg.get('intent') for msg in history[-10:] if msg.get('role') == 'user']  # Últimos 10 user
    refinamientos = recent_intents.count("BUSCAR_MAS")
    # FIX: Threshold más alto (>5) + reset si email ya enviado (evita cierre prematuro)
    if refinamientos > 5 and intent == "BUSCAR_MAS" and not alert_email_sent:
        urgency = "alta"
        criteria['escalar_humano'] = True
        print(f"[ALERTA] Boost escalada por {refinamientos} refinamientos: {phone}")
    else:
        if alert_email_sent:
            criteria['escalar_humano'] = False  # Reset para continuar refinando post-escalada
            print(f"[LOG] Reset escalar_humano por email ya enviado; continúa refinando.")
    
    # NUEVO FIX: Detecta confirmación corta para evitar procesamiento redundante y duplicados
    prev_criteria = load_current_criteria(phone)
    is_short_confirmation = len(message.strip()) <= 5 and any(word in message.lower() for word in ['venta', 'vent', 'arriendo', 'arrendar', 'casa', 'depto', 'condes', 'providencia', 'en', 'si'])
    criteria_keys = ['operacion', 'tipo', 'comuna']
    criteria_match = all(criteria.get(k) == prev_criteria.get(k) for k in criteria_keys if criteria.get(k) is not None)
    core_count = sum(1 for k in criteria_keys if criteria.get(k) and criteria.get(k) != '')

    if intent == "BUSCAR_MAS" and core_count == 3 and criteria_match and is_short_confirmation:
        print(f"[LOG] Modo confirmación detectado: Mensaje corto '{message}' con criteria iguales. Enviando ACK simple.")
        # Append user_msg mínimo
        user_timestamp = extras.get('timestamp', datetime.now(timezone.utc)) if extras else datetime.now(timezone.utc)
        user_msg = {
            "role": "user", 
            "content": message, 
            "timestamp": user_timestamp,
            "intent": "CONFIRMACION",  # Flag especial
            "urgency": "baja",
            **(extras or {})
        }
        if not history or history[-1]["role"] != "user" or history[-1]["content"] != message:
            history.append(user_msg)
        
        # ACK response hardcoded, personalizado con criteria
        operacion_ex = criteria.get('operacion', 'tu búsqueda').title()
        tipo_ex = criteria.get('tipo', 'propiedad')
        comuna_ex = criteria.get('comuna', 'tu zona')
        response = f"¡Perfecto, {message.strip().title()} confirmado para {operacion_ex} de {tipo_ex} en {comuna_ex}! 😊 Ya te mostré opciones geniales. ¿Cuál te interesa más, o agregamos filtros como dormitorios/precio para refinar?"
        ai_msg = {"role": "assistant", "content": response, "timestamp": datetime.now(timezone.utc), "criteria": criteria}
        history.append(ai_msg)
        save_chat_history(phone, history)
        print(f"[BOT -> {phone}] {response} (ACK por confirmación)")
        return True

    # FIX #1: Fuerza "buscar_mas" solo si NO es intent prioritario y criteria ==3 (todos cores)
    priority_intents = ["VENDER_MI_CASA", "AGENDAR_VISITA", "SOLICITUD_CONTACTO", "CIERRE_GRACIAS", "PREGUNTA_NO_RESPONDIBLE"]
    if intent not in priority_intents:
        # FIX: Ajuste para comuna múltiple (cuenta como 1 core si tiene comas)
        core_keys = ['operacion', 'tipo', 'comuna']
        core_count = 0
        for k in core_keys:
            val = criteria.get(k)
            if val and val != '':
                if k == 'comuna' and isinstance(val, str) and ',' in val:
                    core_count += 1  # Lista OR cuenta como completo
                else:
                    core_count += 1
        if core_count == 3:  # ==3 para requerir todos (operacion/tipo exclusivos)
            intent = "BUSCAR_MAS"
            print(f"[LOG] Intent forzado a 'buscar_mas' por 3/3 criterios (no prioritario).")
        else:
            print(f"[LOG] Intent NO forzado: Solo {core_count}/3 cores.")
    
    # FIX #4: Si intent urgente (vender/agendar/contacto), set urgencia="alta" y log alerta
    if intent in ["VENDER_MI_CASA", "AGENDAR_VISITA", "SOLICITUD_CONTACTO"] or criteria.get('escalar_humano', False):
        urgency = "alta"
        print(f"[ALERTA URGENTE] Lead caliente para {phone}: {intent} - Escalar humano (asesor contacta en 5 min).")
    
    # FIX #4: Escala si alta + faltantes
    missing_cores = [k for k in config.CORE_KEYS if k not in criteria or not criteria[k]]
    if urgency == "alta" and len(missing_cores) > 0:
        criteria['escalar_humano'] = True
        print(f"[ALERTA] Escala por urgencia alta + faltantes: {missing_cores}")
    
    # Agrega metadata a user_msg
    user_timestamp = extras.get('timestamp', datetime.now(timezone.utc)) if extras else datetime.now(timezone.utc)
    user_msg = {
        "role": "user", 
        "content": message, 
        "timestamp": user_timestamp,
        "intent": intent, 
        "urgency": urgency,
        **(extras or {})  # Agrega msg_id/timestamp si vienen de webhook
    }
    if not history or history[-1]["role"] != "user" or history[-1]["content"] != message:
        history.append(user_msg)
    
    # FIX: Si necesita_mas_info pero TODOS cores presentes, set False (no solo por comuna)
    missing_cores = [key for key in config.CORE_KEYS if key not in criteria or not criteria[key]]
    necesita_mas_info = criteria.get('necesita_mas_info', False)
    if necesita_mas_info and len(missing_cores) == 0:
        criteria['necesita_mas_info'] = False
        print(f"[LOG] Forzado necesita_mas_info=False por todos cores presentes.")
    else:
        print(f"[LOG] Mantiene necesita_mas_info={criteria.get('necesita_mas_info', False)} (faltan: {missing_cores}).")

    print(f"[LOG] Core keys faltantes: {missing_cores}")
    
    semantic_prefs = criteria.get("prefs_semánticas", "") or ""
    if isinstance(semantic_prefs, list):
        semantic_prefs = ' '.join(semantic_prefs) if semantic_prefs else ''
    print(f"[DEBUG] Semantic prefs pasado a search: '{semantic_prefs}'")
    properties = []
    semantic_scores = []
    recommended_codes = get_recommended_codes(phone)
    print(f"[LOG] Códigos recomendados previos: {len(recommended_codes)}")
    
    # FIX #1: Busca solo si intent=="buscar_mas" y cores ==3 (ignora features para inicial)
    core_count = 0  # Recalcula aquí con el ajuste de comuna múltiple
    for k in config.CORE_KEYS:
        val = criteria.get(k)
        if val and val != '':
            if k == 'comuna' and isinstance(val, str) and ',' in val:
                core_count += 1
            else:
                core_count += 1
    if intent == "BUSCAR_MAS" and core_count == 3:  # ==3, no >=2
        search_criteria = {key: criteria[key] for key in config.CORE_KEYS + config.FEATURE_KEYS 
                           if key in criteria and criteria[key] and criteria[key] != ''}
        if recommended_codes:
            search_criteria['codigo'] = {"$nin": recommended_codes}
        print(f"[LOG] Criterios de búsqueda (con exclude): {search_criteria}")
        props_and_scores = search_properties(search_criteria, semantic_prefs)
        if isinstance(props_and_scores, tuple):
            properties, semantic_scores = props_and_scores
        else:
            properties = props_and_scores
        
        # FIX1: Fallback respeta TODOS criteria (cores + features), solo ignora semantic_prefs/keyword
        if len(properties) == 0:
            print(f"[LOG] Fallback sin prefs/keyword para mostrar top generales en {criteria.get('comuna', 'área')}")
            fallback_criteria = {key: criteria[key] for key in config.CORE_KEYS + config.FEATURE_KEYS 
                                 if key in criteria and criteria[key] and criteria[key] != ''}
            if recommended_codes:
                fallback_criteria['codigo'] = {"$nin": recommended_codes}
            print(f"[LOG] Fallback criteria (con features): {fallback_criteria}")
            props_and_scores_fallback = search_properties(fallback_criteria, "")  # Sin semantic_prefs
            if isinstance(props_and_scores_fallback, tuple):
                properties, semantic_scores = props_and_scores_fallback
            else:
                properties = props_and_scores_fallback
            print(f"[LOG] Fallback encontró: {len(properties)} props generales.")
        
        print(f"[LOG] Propiedades encontradas: {len(properties)} (excluyendo {len(recommended_codes)} previos)")
        
        new_codes = [prop.get('codigo', 'N/A') for prop in properties if prop.get('codigo') not in recommended_codes]
        if new_codes:
            update_recommended_codes(phone, new_codes)
    else:
        if core_count < 3:
            print(f"[LOG] Búsqueda saltada: Solo {core_count}/3 cores (faltan: {missing_cores}).")
        properties = []  # No buscar

    # Pasa is_first_message y feedback_mode a generate_response
    response = generate_response(criteria, history, message, properties, semantic_scores, intent, urgency, is_first_message, feedback_mode=feedback_mode)

    # NUEVO: Guard contra None/empty (por si Grok falla o fallback bug)
    if not response or response.strip() == "" or response == "None":
        response = "¡Ups! Algo salió mal al procesar tu mensaje. 😅 Cuéntame de nuevo qué buscas en propiedades (ej. casa en venta en Las Condes), y te ayudo ya."
        print(f"[ERROR] generate_response devolvió vacío/None para {phone}; usando default.")
        print(f"[LOG] Prompt debug (último): intent={intent}, criteria={clean_for_json(criteria)[:100]}...")

    # NUEVO: Si full_reset, usa {} para current_criteria; sino, criteria actual
    ai_msg = {"role": "assistant", "content": response, "timestamp": datetime.now(timezone.utc), "criteria": {} if do_full_reset else criteria}
    history.append(ai_msg)

    print(f"[BOT -> {phone}] {response}")
    return True

if __name__ == "__main__":
    print("=== Chatbot Inmobiliario Procasa - Modo Consola ===")
    print(f"Usando teléfono de prueba desde .env: {config.TEST_PHONE}")
    print("Envía mensajes directamente. Para salir: 'exit'")
    
    phone = config.TEST_PHONE
    
    while True:
        message = input(f"\nMensaje de {phone}: ").strip()
        if message.lower() == 'exit':
            break
        if message:
            history = load_chat_history(phone)
            # NUEVO: Si no hay historial previo, inicializa con template de feedback
            if len(history) == 0:
                history = initialize_feedback_history(phone)
            success = process_message(phone, message, history=history)
            if success:
                save_chat_history(phone, history)
        else:
            print("[LOG] Mensaje vacío ignorado.")
    
    print("Sesión terminada.")