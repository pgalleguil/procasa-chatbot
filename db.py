import os
import pymongo
import unicodedata
import re
from datetime import datetime, timezone
from config import Config
from bson import ObjectId  # Para _id en Mongo
from email_utils import send_gmail_alert  # NUEVO: Importa función de email


from bson import ObjectId
from datetime import datetime

def clean_for_json(obj):
    """
    Limpia dict/list para json.dumps: ObjectId → str, datetime → ISO str.
    """
    if isinstance(obj, dict):
        return {k: clean_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_for_json(item) for item in obj]
    elif isinstance(obj, ObjectId):
        return str(obj)
    elif isinstance(obj, datetime):
        return obj.isoformat()  # e.g., "2025-11-12T15:55:00+00:00"
    else:
        return obj

config = Config()

client = pymongo.MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]

CHATS_COLLECTION = db["chats"]
PROPERTIES_COLLECTION = db[config.COLLECTION_NAME]
print(f"[LOG] Conectado a colección de propiedades: {config.COLLECTION_NAME}")

def normalize_text(text: str) -> str:
    if not text:
        return ""
    normalized = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    return normalized.lower()

def get_synonyms(field: str, value: str) -> list:
    norm_val = normalize_text(value)
    synonyms = [value.lower(), norm_val]
    
    if field == 'tipo':
        if any(word in norm_val for word in ['departamento', 'depa']):
            synonyms += ['departamento', 'depto', 'dept', 'depa', 'deptos', '1 ambiente', 'estudio']
        elif any(word in norm_val for word in ['casa']):
            synonyms += ['casa', 'residencial', 'hogar']
    elif field == 'operacion':
        if any(word in norm_val for word in ['arriendo', 'renta']):
            synonyms += ['arriendo', 'arriendo temporal', 'renta', 'leasing']
        elif any(word in norm_val for word in ['venta', 'compra']):
            synonyms += ['venta', 'compra', 'traspaso']
    elif field == 'comuna':
        synonyms += [norm_val.replace('ñ', 'n')]  # e.g., ñuñoa -> nuñoa
        # Synonyms comunes (escalable, no exhaustivo; prompt maneja fuzzy)
        if any(word in norm_val for word in ['concon', 'concón']):
            synonyms += ['concón', 'concon', 'con con']
        if any(word in norm_val for word in ['vina del mar', 'viña del mar']):
            synonyms += ['viña del mar', 'vina del mar', 'viña', 'vdm']
    
    return list(set(synonyms))

def save_chat_history(phone: str, messages: list):
    # FIX: Limpia messages para serialización (ObjectId/datetime → str) antes de procesar/guardar
    clean_messages = [clean_for_json(msg) for msg in messages]
    
    # Detecta lead_type global basado en intents recientes (últimos 3 user msgs)
    recent_user_intents = [msg.get('intent') for msg in clean_messages[-6:] if msg.get('role') == 'user']  # Cada 2 msgs = 1 user
    lead_type = "comprador"  # Default
    if any(it in recent_user_intents for it in ["vender_mi_casa", "pregunta_no_respondible"]):
        lead_type = "vendedor" if "vender_mi_casa" in recent_user_intents else "lead_caliente"
    
    # Lead score (suma puntos para urgencia) - FIX: Incluye intent actual y dinámico + bonus urgency del último user
    lead_score = 0
    if any(it in recent_user_intents for it in ["vender_mi_casa", "pregunta_no_respondible"]):
        lead_score += 10  # Alto para vendedores
    if any(it in ["agendar_visita", "solicitud_contacto"] for it in recent_user_intents):  # FIX: +5 para visitas/contacto
        lead_score += 5
    # FIX: Bonus urgency del ÚLTIMO user_msg (messages[-2] si par, o busca último user)
    last_user_urgency = None
    for msg in reversed(clean_messages[-4:]):  # Chequea últimos 4 para último user
        if msg.get('role') == 'user' and msg.get('urgency'):
            last_user_urgency = msg['urgency']
            break
    if last_user_urgency == 'alta':
        lead_score += 3
    print(f"[LOG] Lead score calculado: {lead_score} (intents: {recent_user_intents}, urgency bonus: {last_user_urgency})")
    
    # Recopila unresolved_queries de intents "pregunta_no_respondible"
    unresolved = [msg['content'] for msg in clean_messages if msg.get('intent') == 'pregunta_no_respondible']
    
    # FIX: Escala humano si flag en últimos 5 msgs O intent prioritario reciente O score alto
    priority_intents = ["VENDER_MI_CASA", "AGENDAR_VISITA", "SOLICITUD_CONTACTO"]
    has_priority_intent = any(it in recent_user_intents[-3:] for it in priority_intents)  # Histórico para persistir urgencia
    
    # NUEVO: current_priority_intent solo para decidir re-envío (último user msg)
    current_priority_intent = recent_user_intents[-1] if recent_user_intents else None
    is_current_priority = current_priority_intent in priority_intents if current_priority_intent else False
    
    escalar_humano = (
        any(msg.get('criteria', {}).get('escalar_humano', False) for msg in clean_messages[-5:]) or  # Expande a -5
        has_priority_intent or  # Histórico para escalar
        lead_score > 5
    )
    
    # Guarda current_criteria del último ai_msg (si existe)
    current_criteria = {}
    if clean_messages and clean_messages[-1].get('role') == 'assistant' and 'criteria' in clean_messages[-1]:
        current_criteria = clean_for_json(clean_messages[-1]['criteria'])  # ← FIX: Limpia criteria para email/DB
    
    # do_not_contact default False
    doc = CHATS_COLLECTION.find_one({"phone": phone})
    do_not_contact = doc.get("do_not_contact", False) if doc else False
    
    # NUEVO: Chequea si ya se envió email de alerta para esta escalada
    alert_email_sent = doc.get("alert_email_sent", False) if doc else False
    
    # NUEVO: Obtiene _id antes del upsert (para email)
    chat_id = doc.get("_id") if doc else None
    
    # NUEVO: Obtén recommended_codes y full_history para email
    recommended_codes = get_recommended_codes(phone)  # De tu función existente
    full_history = clean_messages  # ← FIX: Usa clean para evitar issues en email_utils

    # FIX: Envía solo si escalar_humano Y (no enviado O current es priority) — evita ecos históricos
    if escalar_humano and config.GMAIL_USER and config.GMAIL_PASSWORD:
        if not alert_email_sent or is_current_priority:  # Re-envía SOLO si NUEVO intent caliente (no histórico)
            # Obtiene mensaje del cliente (último user msg)
            last_user_msg = next((msg['content'] for msg in reversed(clean_messages) if msg.get('role') == 'user'), 'No disponible')
            # Historial breve (últimos 4 user msgs para recent_history)
            recent_user_msgs = [msg['content'] for msg in clean_messages[-8:][::-1] if msg.get('role') == 'user'][:4]  # FIX: Más extendido (4 user)
            recent_history = ' | '.join(recent_user_msgs) if recent_user_msgs else 'Sin historial previo'
            send_gmail_alert(phone, lead_type, lead_score, current_criteria, clean_messages[-1].get('content', ''), last_user_msg, recent_history, str(chat_id) if chat_id else None, full_history=full_history)
            alert_email_sent = True  # Marca para update
            print(f"[LOG] Email de urgencia enviado (primera: {not doc.get('alert_email_sent', False) if doc else True}, current_priority: {is_current_priority})")  # FIX log: Más claro
    
    # Upsert principal
    result = CHATS_COLLECTION.update_one(
        {"phone": phone},
        {"$set": {
            "messages": clean_messages,  # ← FIX: Usa clean_messages para guardar serializable
            "last_updated": datetime.now(timezone.utc),
            **{"lead_type": lead_type},
            **({"lead_score": lead_score} if lead_score > 0 else {}),
            **({"escalar_humano": escalar_humano} if escalar_humano else {}),
            **({"unresolved_queries": unresolved} if unresolved else {}),
            **{"current_criteria": current_criteria},
            **{"do_not_contact": do_not_contact},
            **{"active": True},  # Default active, overridden en chatbot.py
            **({"alert_email_sent": alert_email_sent} if escalar_humano else {}),  # Flag para evitar re-envíos
        }},
        upsert=True
    )
    
    # NUEVO: Si es insert (nuevo chat), obtén el _id generado
    if result.upserted_id:
        chat_id = result.upserted_id
    
    # NUEVO: Registra envío de email en array histórico (para seguimiento)
    if escalar_humano and alert_email_sent:
        email_log = {
            "timestamp": datetime.now(timezone.utc),
            "type": "escalada_humano",
            "lead_score": lead_score,
            "phone": phone,
            "chat_id": str(chat_id) if chat_id else None
        }
        CHATS_COLLECTION.update_one(
            {"phone": phone},
            {"$push": {"email_alerts_sent": email_log}}
        )
        print(f"[LOG] Registro de email agregado para {phone}: {email_log}")
    
    if escalar_humano:
        print(f"[ALERTA URGENTE] {phone}: Lead score {lead_score}, escalar a humano! (Email enviado: {alert_email_sent}, ID: {chat_id})")
    print(f"[LOG] Historial guardado para {phone} en MongoDB (lead_type: {lead_type}, score: {lead_score}, escalar: {escalar_humano}).")

def load_chat_history(phone: str) -> list:
    doc = CHATS_COLLECTION.find_one({"phone": phone})
    if doc and "messages" in doc:
        messages = doc["messages"]
        # Normaliza y PARSEA timestamps a datetime
        for msg in messages:
            ts_raw = msg.get("timestamp")
            if isinstance(ts_raw, str):
                try:
                    msg["timestamp"] = datetime.fromisoformat(ts_raw.replace('Z', '+00:00'))
                except ValueError:
                    msg["timestamp"] = datetime.now(timezone.utc)
            elif isinstance(msg.get("timestamp"), datetime) and msg["timestamp"].tzinfo is None:
                msg["timestamp"] = msg["timestamp"].replace(tzinfo=timezone.utc)
        # NO uses clean_for_json aquí si quieres datetime; úsalo solo para logs/exports
        return messages  # Retorna con datetime reales
    print(f"[LOG] No hay historial previo para {phone}. Iniciando nuevo.")
    return []

# Func para load current_criteria de DB
def load_current_criteria(phone: str) -> dict:
    doc = CHATS_COLLECTION.find_one({"phone": phone})
    criteria = doc.get("current_criteria", {}) if doc else {}
    if not criteria:  # FIX: Fallback a último msg con criteria en history
        history = load_chat_history(phone)
        for msg in reversed(history):
            if msg.get("role") == "assistant" and "criteria" in msg and msg["criteria"]:
                criteria = msg["criteria"]
                print(f"[LOG] Criteria fallback de history[-1] para {phone}: {criteria}.")
                break
    print(f"[LOG] Current_criteria cargado (con fallback) para {phone}: {criteria}.")
    return criteria

def get_recommended_codes(phone: str) -> list:
    doc = CHATS_COLLECTION.find_one({"phone": phone})
    return doc.get("recommended_codes", []) if doc else []

def update_recommended_codes(phone: str, new_codes: list):
    doc = CHATS_COLLECTION.find_one({"phone": phone})
    if doc:
        codes = list(set(doc.get("recommended_codes", []) + new_codes))[:20]
        CHATS_COLLECTION.update_one(
            {"phone": phone},
            {"$set": {"recommended_codes": codes, "last_updated": datetime.now(timezone.utc)}}
        )
        print(f"[LOG] Códigos recomendados actualizados para {phone}: {len(codes)} total.")
    else:
        CHATS_COLLECTION.insert_one({
            "phone": phone,
            "recommended_codes": new_codes,
            "last_updated": datetime.now(timezone.utc)
        })

def get_properties_filtered(criteria: dict, semantic_prefs: str = "", max_docs: int = config.MAX_DOCS) -> list:
    and_conditions = []
    
    # Siempre agregar filtro disponible: true
    and_conditions.append({"disponible": True})
    print(f"[LOG] Filtro 'disponible: true' aplicado obligatoriamente.")
    
    # Core keys (SALTA 'comuna' para manejarla por separado y evitar duplicado)
    for key in config.CORE_KEYS:
        if key == 'comuna':  # FIX: Salta, maneja abajo
            continue
        if key in criteria and criteria[key]:
            val = str(criteria[key])
            synonyms = get_synonyms(key, val)
            or_conditions = [ {key: {"$regex": syn, "$options": "i"}} for syn in synonyms ]
            and_conditions.append({"$or": or_conditions})
    
    # Comuna (single or multiple, NO duplicate) - FIX: Mejor split para "Las Condes, Providencia"
    comuna_condition_added = False
    if 'comuna' in criteria and criteria['comuna']:
        comuna_str = str(criteria['comuna']).lower()
        is_multiple = False
        if ',' in comuna_str:
            is_multiple = True
            comunas = [c.strip() for c in comuna_str.split(',') if c.strip()]  # FIX: Split simple por ',', clean
            comuna_ors = []
            for c in comunas:
                syns = get_synonyms('comuna', c)
                comuna_ors.extend([{ "comuna": {"$regex": syn, "$options": "i"} } for syn in syns])
            if comuna_ors:
                and_conditions.append({"$or": comuna_ors})
                comuna_condition_added = True
                print(f"[LOG] Filtro multiple comunas: {comunas}")
        if not is_multiple:
            syns = get_synonyms('comuna', comuna_str)
            or_conditions = [{ "comuna": {"$regex": syn, "$options": "i"} } for syn in syns]
            and_conditions.append({"$or": or_conditions})
            comuna_condition_added = True
            print(f"[LOG] Filtro single comuna: {comuna_str} (syns: {len(syns)})")
    
    # Features numéricas y string (con operadores Mongo)
    for key in config.FEATURE_KEYS:
        if key in criteria and criteria[key]:
            val = criteria[key]
            if isinstance(val, dict):  # Operadores como {"$gte": 2}
                op = list(val.keys())[0]
                num_val = val[op]
                if op in ['$eq', '$gte', '$lte', '$gt', '$lt']:
                    # Usa $expr para comparación numérica segura (convierte strings a int)
                    and_conditions.append({
                        "$expr": {
                            op: [
                                {"$toInt": f"${key}"},  # Convierte a int (ignora nulls)
                                num_val
                            ]
                        }
                    })
                    print(f"[LOG] Operador $expr para {key}: {val} (mixed types)")
            elif isinstance(val, (int, float)):  # Numérico simple
                and_conditions.append({key: {"$in": [val, str(val)]}})
            else:  # String, usa regex
                synonyms = get_synonyms(key, str(val))
                or_conditions = [{key: {"$regex": syn, "$options": "i"}} for syn in synonyms]
                and_conditions.append({"$or": or_conditions})
    
    # Exclusión
    if 'codigo' in criteria:
        and_conditions.append({'codigo': criteria['codigo']})
        print(f"[LOG] Exclusión aplicada: {len(criteria['codigo']['$nin'])} códigos previos en query.")
    
    # Keyword para prefs_semánticas - FIX: Híbrido para multi-prefs (OR positivas, NOR negativas, AND si >2 locativas)
    prefs_semánticas = semantic_prefs.lower().strip()
    print(f"[DEBUG] Prefs semánticas pasado a DB: '{prefs_semánticas}'")
    if prefs_semánticas:
        prefs_semánticas = re.sub(r'psicina|psicna', 'piscina', prefs_semánticas)
        # NUEVO: Limpia comas y términos irrelevantes (precios, max/min, operacion/tipo)
        prefs_semánticas = re.sub(r'[,\.]', ' ', prefs_semánticas)  # Quita comas/puntos
        exclude_terms = ['venta', 'arriendo', 'departamento', 'casa', 'máximo', 'mínimo', 'desde', 'hasta', 'uf', r'\d+']  # Regex para números
        for term in exclude_terms:
            if isinstance(term, str):
                prefs_semánticas = re.sub(rf'\b{re.escape(term)}\b', '', prefs_semánticas)
            else:  # Para regex como \d+
                prefs_semánticas = re.sub(term, '', prefs_semánticas)
        prefs_semánticas = re.sub(r'\s+', ' ', prefs_semánticas).strip()  # Normaliza espacios
        print(f"[DEBUG] Prefs semánticas limpiada para keywords: '{prefs_semánticas}'")
        
        # Split por comas/y/etc para positives/negatives
        prefs_list = re.split(r',|y|pero|que no', prefs_semánticas)
        prefs_list = [p.strip() for p in prefs_list if p.strip()]
        positive_prefs = [p for p in prefs_list if any(w in p for w in ['cerca', 'proximo', 'a pasos'])]
        negative_prefs = [p for p in prefs_list if any(w in p for w in ['lejos', 'sin', 'evitar', 'alejado'])]
        
        # Positivas: OR de todas (menos estricto) - FIX: Split por palabras después de limpiar
        if positive_prefs:
            pos_kws = []
            for pref in positive_prefs:
                # Limpia fillers de locación
                kw = re.sub(r'\b(cerca|proximo|a pasos|del|un|la|de|en|las|los)\b', '', pref).strip()
                if kw:
                    # Split por espacios y toma palabras >2 letras
                    kw_words = [w for w in kw.split() if len(w) > 2]
                    pos_kws.extend(kw_words)
            if pos_kws:
                # OR por cada keyword individual
                pos_or = {"$or": [{"$or": [{"descripcion": {"$regex": kw, "$options": "i"}}, {"amenities": {"$regex": kw, "$options": "i"}}, {"descripcion_clean": {"$regex": kw, "$options": "i"}}]} for kw in pos_kws]}
                and_conditions.append(pos_or)
                print(f"[LOG] Filtro OR positivas para {pos_kws}")
        
        # Negativas: NOR de cada una (evita si ANY field match) - FIX: Split por palabras
        if negative_prefs:
            neg_nor = {"$nor": []}
            for pref in negative_prefs:
                kw = re.sub(r'\b(lejos|sin|evitar|del|un|la|de|en|las|los)\b', '', pref).strip()
                if kw:
                    kw_words = [w for w in kw.split() if len(w) > 2]
                    for word in kw_words:
                        neg_or = {"$or": [{"descripcion": {"$regex": word, "$options": "i"}}, {"amenities": {"$regex": word, "$options": "i"}}, {"descripcion_clean": {"$regex": word, "$options": "i"}}]}
                        neg_nor["$nor"].append(neg_or)
            if neg_nor["$nor"]:
                and_conditions.append(neg_nor)
                neg_regex = r'\b(lejos|sin|evitar|del|un|la|de|en|las|los)\b'
                neg_words = [w for pref in negative_prefs for w in re.sub(neg_regex, '', pref).strip().split() if len(w) > 2]
                print(f"[LOG] Filtro NOR negativas para {neg_words}")
        # Si >2 prefs, agrega AND genérico para location (opcional, si quieres estricto)
        if len(prefs_list) > 2:
            location_and = {"$and": [{"descripcion": {"$regex": "ubicacion|sector|cerca|proximo", "$options": "i"}}, {"$expr": {"$gt": [{"$strLenCP": "$descripcion"}, 50]}}]}  # Asegura desc detallada
            and_conditions.append(location_and)
            print(f"[LOG] Filtro AND genérico para multi-prefs (>2): location detallada")
    
    query = {"$and": and_conditions} if len(and_conditions) > 1 else and_conditions[0] if and_conditions else {}
    
    normalized_criteria = {k: normalize_text(v) if isinstance(v, str) else v for k, v in criteria.items()}
    print(f"[LOG] Criterios normalizados para query: {normalized_criteria}")
    print(f"[LOG] Query Mongo construida: {query}")
    
    pipeline = [
        {"$match": query},
        {"$addFields": {
            "boost_score": {
                "$cond": {
                    "if": {"$eq": ["$oficina", config.PRIORITY_OFICINA]},
                    "then": config.PRIORITY_BOOST,
                    "else": 0
                }
            }
        }},
        {"$limit": max_docs}
    ]
    
    try:
        results = list(PROPERTIES_COLLECTION.aggregate(pipeline))
        print(f"[LOG] Propiedades filtradas: {len(results)} resultados para criteria {criteria} (prefs: '{semantic_prefs}').")
        if results:
            dorm_samples = [p.get('dormitorios', 'N/A') for p in results[:3]]
            print(f"[LOG] Dorms en results (primeros 3): {dorm_samples}")
            if prefs_semánticas and 'pos_kws' in locals():  # Usa pos_kws si definido
                matches_count = sum(1 for p in results if any(kw in (p.get('descripcion', '') + p.get('amenities', '') + p.get('descripcion_clean', '')).lower() for kw in pos_kws))
                print(f"[LOG] Matches con '{prefs_semánticas}': {matches_count}/{len(results)} props.")
    except pymongo.errors.OperationFailure as e:
        print(f"[ERROR] Fallo en aggregate: {e}. Usando fallback find...")
        results = list(PROPERTIES_COLLECTION.find(query).limit(max_docs))
        print(f"[LOG] Fallback find: {len(results)} resultados.")
        if results and prefs_semánticas and 'pos_kws' in locals():
            matches_count = sum(1 for p in results if any(kw in (p.get('descripcion', '') + p.get('amenities', '') + p.get('descripcion_clean', '')).lower() for kw in pos_kws))
            print(f"[LOG] Matches con '{prefs_semánticas}' en fallback: {matches_count}/{len(results)}.")
    
    if len(results) == 0:
        sample = list(PROPERTIES_COLLECTION.find({}, {'operacion':1, 'tipo':1, 'comuna':1, 'dormitorios':1}).limit(3))
        print(f"[LOG] Sample de DB para debug (primeros 3 docs en {config.COLLECTION_NAME}):")
        for s in sample:
            print(f"  - operacion: '{s.get('operacion', 'N/A')}', tipo: '{s.get('tipo', 'N/A')}', comuna: '{s.get('comuna', 'N/A')}', dorm: {s.get('dormitorios', 'N/A')}")
        total = PROPERTIES_COLLECTION.count_documents({})
        print(f"[LOG] Total propiedades en colección: {total}")
        if prefs_semánticas:
            print(f"[LOG] 0 results con keyword '{prefs_semánticas}'. Sugerencia: Ignorar filtro y usar semántica pura.")
    
    return results