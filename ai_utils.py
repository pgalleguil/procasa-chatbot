# ai_utils.py
# Utilidades para Grok (xAI): prompts, análisis, extracción, generación
# Migrado de Gemini a xAI/Grok API (compatible con OpenAI format)
# OPTIMIZADO: Prompts reducidos ~25-30%, wrapper process_message para ahorro en flujos negativos/positivos
# FIX GRAVE: En generate_response, si necesita_mas_info=True, fuerza fallback que SOLO pregunta por cores, SIN recomendaciones ni props inventadas.
# NUEVO FIX: Si escalar_humano=True (e.g., visita/vender/contacto), NO pide más datos (nombre/etc.); solo agradece, confirma email/contacto inminente, y cierra diálogo cortésmente (sin CTA abierta).
# FIX CORE_KEYS: Validación estricta de explicitud en CORE_KEYS (config.py); features opcionales no bloquean.
# FIX HÍBRIDO CONTEXTO: No resetea cores heredados de previous si explícitos en full_context (historial + mensaje); solo inferidos raw.
# FIX PRECIO HERENCIA: No resetea precio_uf heredado si 'uf' en full_context (historial); solo si nuevo sin mención.
# NUEVO: Soporte para feedback_mode (detecta outreach template en history[0]) para boostear intents y ajustar respuestas en leads tibios.

import requests
import json
import re
from typing import Dict, List, Any
from config import Config, config  # Asegura config para CORE_KEYS/FEATURE_KEYS
from db import load_current_criteria  # Para cargar criteria previos de DB

from bson import ObjectId  # Ya importado si usas Mongo
from datetime import datetime

config = Config()

def clean_for_json(obj):
    if isinstance(obj, dict):
        return {k: clean_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_for_json(item) for item in obj]
    elif isinstance(obj, ObjectId):
        return str(obj)
    elif isinstance(obj, datetime):
        return obj.isoformat()
    else:
        return obj

def call_grok(prompt: str, system_prompt: str = None, max_tokens: int = 1024) -> str:
    if not config.XAI_API_KEY:
        raise ValueError("XAI_API_KEY no configurada en .env")
    
    url = f"{config.GROK_BASE_URL}/chat/completions"
    messages = [{"role": "user", "content": prompt}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    
    payload = {
        "model": config.GROK_MODEL,
        "messages": messages,
        "temperature": config.GROK_TEMPERATURE,
        "max_tokens": max_tokens,
        "stream": False
    }
    
    headers = {
        "Authorization": f"Bearer {config.XAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        if 'choices' not in data or not data['choices']:
            raise ValueError(f"Respuesta inválida de Grok: {data.get('error', 'No choices')}")
        return data['choices'][0]['message']['content'].strip()
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Llamada a Grok falló: {e}")
        raise
    except (KeyError, IndexError, ValueError) as e:
        print(f"[ERROR] Formato de respuesta inválido de Grok: {e}")
        raise

# === ACTUALIZADO: Helper para validación de explicitud en cores (usa full_context) ===
# NUEVO (ANÁLISIS): Keywords expandidos para comunas/barrios (e.g., 'cerrillos', 'la cisterna')
def is_explicit_core(key: str, message: str, criteria_value: Any, history_summary: str = '') -> bool:
    """
    Chequea si un core es explícito en full_context (historial + mensaje).
    Retorna True solo si criteria_value no es None Y hay keyword matching.
    """
    if criteria_value is None:
        return False
    
    full_context = (history_summary + ' ' + message).lower()
    explicit_keywords = {
        'operacion': ['venta', 'compra', 'comprar', 'arriendo', 'alquiler', 'arrendar', 'alquilar', 'vivir', 'mensual'],  # ← Agrega estos 2
        'tipo': ['departamento', 'depto', 'dept', 'casa', 'estudio', '1 ambiente', 'monoambiente'],
        'comuna': ['providencia', 'ñuñoa', 'nunoa', 'las condes', 'vitacura', 'santiago', 'independencia', 'recoleta', 'viña del mar', 'concón', 'cerrillos', 'la cisterna', 'el bosque']  # ← Agrega estos 3
    }
    
    kws = explicit_keywords.get(key, [])
    return any(kw in full_context for kw in kws)

# === NUEVO: Detecta feedback_mode si historial inicia con template de outreach ===
def is_feedback_mode(history: List[Dict]) -> bool:
    """
    Chequea si es un chat de feedback post-outreach (template en history[0]).
    """
    if history and len(history) > 0 and history[0].get("role") == "assistant":
        template_content = history[0]["content"].lower()
        return "recordamos tu interés" in template_content or "¿pudiste conocerla?" in template_content or "qué te pareció" in template_content
    return False

# === SENTIMENT PROMPT === (Optimizado: + Regla para incompletos)
SENTIMENT_PROMPT = """
Analiza el sentimiento del mensaje sobre interés en propiedades.

Reglas:
- POSITIVE: interés, neutral o refinamiento (ej. "solo 2 dorms", "¿en Viña?", "vent" como posible "venta").
- NEGATIVE: rechazo explícito, molestia o loop/frustración (ej. "no más", "cancela", "ya te lo dije", "basta"). Incompletos cortos (<4 chars) sin keywords negativos → POSITIVE.
- Ignora condicionales neutras en refinamientos.

Mensaje: {message}
Respuesta: (solo POSITIVE o NEGATIVE)
"""

def analyze_sentiment(message: str) -> str:
    """
    Clasifica sentimiento como POSITIVE o NEGATIVE usando Grok.
    FIX: Pre-chequeo para incompletos.
    """
    # Pre-chequeo local: Cortos sin negative words → POSITIVE (evita falsos negativos en typos)
    short_msg = len(message.strip()) < 4
    negative_local = any(word in message.lower() for word in ['no', 'cancela', 'basta', 'molesta', 'frustrado'])
    if short_msg and not negative_local:
        print(f"[LOG] Sentiment skipped por corto/no-negative: '{message}' → POSITIVE. Ahorro: 1 llamada.")
        return "POSITIVE"
    
    prompt = SENTIMENT_PROMPT.format(message=message)
    response = call_grok(prompt, max_tokens=50)
    return response.strip().upper()

# === INTENT PROMPT === (Optimizado: ~25% más corto; + FIX: Mejor disambiguación para mixed feedback)
INTENT_PROMPT = """
Clasifica la INTENCIÓN principal en UNA categoría EXACTA para chatbot inmobiliario:

- AGENDAR_VISITA: interés en visitar/agendar (ej. "me interesa Cód. XXX").
- BUSCAR_MAS: más opciones/refinamiento (ej. "tienes más?", "3 dorms", "venta" post-"busco").
- VENDER_MI_CASA: vender/tasación propia (ej. "vendo mi casa" – no "venta" en compra).
- SOLICITUD_CONTACTO: contacto directo (ej. "llámame", "asesor").
- CORRECCION: rectifica dato (ej. "son 2 dorms").
- FEEDBACK_VISITA: comentario post-visita, positivo O NEGATIVO con interés continuo (ej. "me gustó", "no me gustó pero sigo buscando", "visité y quiero algo más grande").
- PREGUNTA_NO_RESPONDIBLE: legal/financiera compleja (ej. "crédito?").
- CIERRE_GRACIAS: cierre neutral (ej. "gracias").
- RECHAZO: no interés fuerte, SIN continuación (ej. "no más", "no me interesa nada").
- EN_PROCESO: ya con ejecutivo (ej. "tengo cita").
- OTRO: sin intención clara.

Disambiguación:
- "Venta"/"arriendo" en contexto "busco" → BUSCAR_MAS.
- Feedback mixto (negativo + "sigo buscando"/"quiero más") → FEEDBACK_VISITA (oportunidad de refinamiento).
- Prioridad: AGENDAR_VISITA > VENDER_MI_CASA > SOLICITUD_CONTACTO > FEEDBACK_VISITA > RECHAZO > EN_PROCESO > resto.

Historial: {history_summary}
Mensaje: {message}

Responde SOLO con categoría en mayúscula.
"""

def classify_intent(history: list, message: str) -> str:
    history_summary = " ".join([msg["content"] for msg in history[-config.HISTORIAL_MAX:] if msg["role"] != "assistant"])
    response = call_grok(INTENT_PROMPT.format(history_summary=history_summary, message=message))
    return response.strip().upper()

# === URGENCY PROMPT === (Optimizado: ~28% más corto)
URGENCY_PROMPT = """
Clasifica urgencia en contexto inmobiliario:

- alta: inmediata/frustración intensa (ej. "ya", "urgente", "mañana").
- media: normal/sin presión (ej. "pronto").
- baja: casual (ej. "después").
- Frustración/repetición → al menos media.

Historial: {history_summary}
Mensaje: {message}

Responde SOLO con: alta, media o baja.
"""

def classify_urgency(history: list, message: str) -> str:
    recent = history[-4:] if history else []
    clean_recent = [{"role": m["role"], "content": m["content"]} for m in recent]
    history_summary = json.dumps(clean_for_json(clean_recent))
    
    prompt = URGENCY_PROMPT.format(history_summary=history_summary, message=message)
    response = call_grok(prompt, max_tokens=50)
    return response.strip().lower()

# === EXTRACTION PROMPT === (Actualizado: Reglas estrictas anti-inferencia para CORE_KEYS)
EXTRACTION_PROMPT = """
Extrae/actualiza criterios de búsqueda inmobiliaria en Chile del mensaje (con errores/abreviaciones).

Reglas ESTRIC TAS:
- Hereda previous_criteria para operacion ("venta"/"arriendo"), tipo ("departamento"/"casa"), comuna SIEMPRE, SOLO si NO hay mención en mensaje; si ambiguo/inferido (ej. "busco" sin "venta/arriendo"), setea None y necesita_mas_info=True.
- NO INFIRAS operacion: Solo 'venta' si explícito ("venta", "compra", "quiero comprar"); 'arriendo' si ("arriendo", "alquiler", "arrendar"). NUEVO: Si 'vivir' o 'mensual' sin explícito, infiere 'arriendo' con flag 'inferido:true'.
- Comuna: Une nombres chilenos (ej. "las condes, vitacura" → "Las Condes, Vitacura"). Lista comma-separated; hereda si no cambia. NUEVO: Mapea barrios/metros (ej. "Cerrillos" → "Cerrillos"; "Metro La Cisterna" → "La Cisterna").
- Precios: Default precio_clp (ej. "hasta 650k" → {{"$lte": 650000}}; "k"=1000, "mil"=1e6, "millones"*1e6). Solo precio_uf si menciona "UF" (ej. "120 UF" → {{"$lte": 120}}). "Entre A y B" → gte/lte.
- Features: dormitorios/banos/estacionamientos (números/rangos con $gte/$lte/$eq).
- prefs_semánticas: Solo preferencias textuales (ej. ["cerca metro", "piscina", "vista al mar"]); NO comunas ni fillers ("busca", "en"). 
- necesita_mas_info: True si CUALQUIER core (operacion/tipo/comuna) es None o inferido (no explícito).
- escalar_humano: True si financiamiento/visita/contacto urgente.

Responde SOLO con JSON:
{{"operacion": "venta" | "arriendo" | null, "tipo": "...", "comuna": "...", "dormitorios": {{"$gte":3}} | null, "banos":2 | null, "estacionamientos":1 | null, "precio_uf": {{"$lte":5000}} | null, "precio_clp": {{"$gte":100000000}} | null, "prefs_semánticas": ["..."] | "", "necesita_mas_info": bool, "escalar_humano": bool, "inferido": {{"operacion": bool, "comuna": bool}}}}  # ← NUEVO: Flag inferido

Historial: {history_summary}
Mensaje: {message}
Previous: {previous_criteria}
"""

# === NUEVA FUNCIÓN: extract_criteria (implementada para completar el flujo) ===
def extract_criteria(history: list, message: str, phone: str = None) -> Dict[str, Any]:
    """
    Extrae criterios usando Grok y post-procesa con herencia de DB.
    """
    previous_criteria = load_current_criteria(phone) if phone else {}
    
    # Toma últimos 6 mensajes para summary
    recent = history[-6:] if history else []
    clean_recent = [{"role": m["role"], "content": m["content"]} for m in recent if "role" in m]
    history_summary = json.dumps(clean_for_json(clean_recent))
    
    prompt = EXTRACTION_PROMPT.format(history_summary=history_summary, message=message, previous_criteria=json.dumps(previous_criteria))
    
    try:
        raw_criteria = json.loads(call_grok(prompt, max_tokens=500))
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[ERROR] Extracción falló (JSON inválido): {e}. Usando previous_criteria.")
        return previous_criteria
    
    # Post-proceso: Herencia + validación explicitud
    criteria = {**previous_criteria, **raw_criteria}  # Merge, prioriza raw
    
    # FIX: Chequea explicitud para cada core
    full_context = history_summary + ' ' + message
    for key in config.CORE_KEYS:
        val = criteria.get(key)
        if val and is_explicit_core(key, message, val, full_context):
            print(f"[LOG] Post-proceso: {key} explícito en full_context → OK.")
        elif val and raw_criteria.get(key) is None:  # Heredado, no explícito
            criteria[key] = None  # Reset si no mencionado
            criteria['necesita_mas_info'] = True
            print(f"[LOG] Post-proceso: {key} heredado pero no explícito → reset a None.")
    
    # NUEVO: Flag inferido desde raw
    inferido = raw_criteria.get('inferido', {})
    criteria['inferido'] = inferido
    
    # Limpia prefs_semánticas si lista vacía
    if criteria.get('prefs_semánticas') == []:
        criteria['prefs_semánticas'] = ''
    
    # Set necesita_mas_info basado en None/inferido
    needs_info = any(criteria.get(k) is None for k in config.CORE_KEYS) or any(inferido.get(k, False) for k in config.CORE_KEYS if k in inferido)
    criteria['necesita_mas_info'] = needs_info
    
    print(f"[LOG] Criterios extraídos (merged + post-process + explicit CORE_KEYS check + inferido): {clean_for_json(criteria)}")
    return criteria

def clean_markdown_links(text: str) -> str:
    pattern = r'\[([^\]]+)\]\(([^\)]+)\)'
    def replace_match(match):
        url = match.group(2).strip()
        if url.startswith('http'):
            return f"🔗 {url}"
        return match.group(0)
    cleaned = re.sub(pattern, replace_match, text, flags=re.IGNORECASE | re.MULTILINE)
    cleaned = re.sub(r'\[([^\]]+)\](?!\()', r'\1', cleaned)  # Limpia [] sueltos
    return cleaned.strip()

# NUEVO: Helper para limpiar descripciones de propiedades (anti-alucinación) - V4 mejorada
descriptive_starters = [
    r'Muy buena', r'Clásica', r'Excelente', r'Hermosa', r'Moderna', r'Amplia', r'Buena', 
    r'Casa ', r'Departamento ', r'Propiedad ', r'Ubicada ', r'Con ', r'En ', r'Ideal ', 
    r'Tradicional', r'Estilo ', r'Con quincho', r'Piscina', r'Jardín', r'Vista '
]

def clean_description(descripcion: str, dormitorios: int, comuna: str = '') -> str:
    """
    Limpia descripcion: Remueve garbage (códigos, nombres empresa), extrae frase útil.
    Si inválida, genera default. Versión V4 con starters descriptivos.
    """
    if not descripcion or len(descripcion.strip()) < 10:
        return f"Casa con {dormitorios} dorms en {comuna or 'tu zona'}, lista para tu estilo de vida."
    
    # Normalize
    descripcion = re.sub(r'\n+', ' ', descripcion)
    descripcion = ' '.join(descripcion.split())
    
    # First, remove obvious garbage like URLs, codes, headers
    descripcion = re.sub(r'https?://\S+|www\.\S+|Código\s+\d+|Descripción\s*:?\s*', '', descripcion, flags=re.IGNORECASE)
    descripcion = re.sub(r'(PROCASA|ProCasa|Procasa|VENDE|Oficina)\b\s*', '', descripcion, flags=re.IGNORECASE)
    
    # Find the start of descriptive text using starters
    start_pos = len(descripcion)
    for starter in descriptive_starters:
        match = re.search(starter, descripcion, re.IGNORECASE)
        if match:
            start_pos = min(start_pos, match.start())
    
    if start_pos < len(descripcion):
        descripcion = descripcion[start_pos:].strip()
    
    # If no starter found, remove prefix up to first capital word or after 50 chars
    if start_pos == len(descripcion):
        # Fallback: remove first 50 chars or until first period
        match = re.search(r'\.', descripcion)
        if match:
            descripcion = descripcion[match.end():].strip()
        else:
            descripcion = descripcion[80:].strip() if len(descripcion) > 80 else descripcion
    
    descripcion = re.sub(r'\.{3,}', '', descripcion)
    descripcion = ' '.join(descripcion.split())
    
    if len(descripcion) < 10:
        return f"Casa con {dormitorios} dorms en {comuna or 'tu zona'}, lista para tu estilo de vida."
    
    # First sentence
    sentences = re.split(r'[.!?]+', descripcion)
    first = next((s.strip() for s in sentences if len(s.strip()) > 5), '')
    if first:
        # Capitalize properly
        first = first[0].upper() + first[1:] if first and first[0].islower() else first
        if len(first) > 50:  # Cap más estricto
            first = first[:50] + '...'
        return first + '.' if not first.endswith(('.', '!', '?')) else first
    
    return f"Casa con {dormitorios} dorms en {comuna or 'tu zona'}, lista para tu estilo de vida."

# === GENERATE_RESPONSE PRINCIPAL === (Versión revisada: Híbrida natural + anti-aluc + anti-trunc)
def generate_response(criteria: Dict, history: List, message: str, properties: List, semantic_scores: List, intent: str, urgency: str, is_first_message: bool, feedback_mode: bool = False) -> str:

    summary_criteria = f"{criteria.get('tipo', 'propiedad')} {criteria.get('operacion', '')}"
    if criteria.get('prefs_semánticas'):
        summary_criteria += f" con {', '.join(criteria.get('prefs_semánticas', []))}"
    comuna = criteria.get("comuna", "tu zona preferida")


    # NUEVO: Helper para formatear precio_uf (redondea a 3 decimales, quita .000)
    def format_uf_price(precio):
        if isinstance(precio, (int, float)):
            rounded = round(precio, 3)
            if rounded.is_integer():
                return f"{int(rounded):,} UF"  # Ej. "10.000 UF" → "10 UF"
            else:
                return f"{rounded:,.3f} UF"  # Ej. "10383.5191" → "10.384 UF"
        return str(precio) + " UF"  # Fallback si str

    # Slim props para prompt (evita hang por tamaño – solo essentials, cortos)
    slim_props = []
    for prop in properties[:3]:
        slim = {
            'codigo': prop.get('codigo', 'N/A'),
            'precio_uf': format_uf_price(prop.get('precio_uf', 'a consultar')),  # ← AQUÍ: Formatea
            'dormitorios': prop.get('dormitorios', 'el número que buscas'),
            'banos': prop.get('banos', 'los baños ideales'),
            'superficie': prop.get('superficie', 'de generosa extensión'),
            'descripcion_clean': (prop.get('descripcion_clean', prop.get('descripcion', ''))[:80] + '...' if len(prop.get('descripcion', '')) > 80 else prop.get('descripcion', '')),
            'amenities': prop.get('amenities', []),
            'link': f"https://www.procasa.cl/{prop.get('codigo', '')}"
        }
        slim_props.append(slim)

    # === SYSTEM_PROMPT HÍBRIDO: Natural pero Anclado ===
    system_prompt = f"""
Eres un asesor inmobiliario cálido y experto de PROCASA en Chile. Responde en español chileno accesible, como una charla fluida: usa contracciones ('te recomiendo', 'suena genial'), emojis sutiles 😊, y beneficios implícitos de los datos (ej. si 'piscina' en amenities, di 'para refrescar el verano'). Max 200 palabras, \\n\\n entre paras.

Reglas para Naturalidad + Fidelidad:
- Fluye conversacional: Integra props en narrativa (ej. 'Esta casa con piscina me hace pensar en tardes relax en familia'), PERO SOLO beneficios derivados DIRECTO de descripcion_clean o amenities. NO inventes (ej. NO 'cerca de mall' si no en props).
- Estructura suave para props (máx 3): Intro amigable ('¡Genial! Para tu búsqueda de {summary_criteria} en {comuna}, mira estas que encajan perfecto:'). Por prop: Describe fluido (copia base de clean_desc + amenity highlight), link natural ('Chequéala en: [link]'). 
- Si dato falta: Di 'con el espacio que imagino te gusta' – natural, no inventado.
- Siempre CTA engaging: '¿Alguna te llama? Te paso con un asesor para visitarla ya, o dime si ajustamos (ej. más dorms o precio).'
- Si 0 props: 'No hay exactas ahora, pero cerca hay joyitas – ¿ampliamos un poco? Te conecto con experto.'
- Urgencia {urgency}: Sé entusiasta ('¡Rápido, esta es ideal!'). Intent: {intent}. Feedback: {feedback_mode}. Primer msg: {is_first_message}.

Criteria: {json.dumps(criteria)}.
Slim props (ANCLA AQUÍ TODO): {json.dumps(slim_props, ensure_ascii=False)} – Usa EXACTO clean_desc y amenities.

Historia: {json.dumps([{'role': m['role'], 'content': m['content'][:50]} for m in history[-3:]])}.
"""

    # === FLUJO PRINCIPAL: Reemplaza la llamada genérica con esto ===
    print(f"[DEBUG] Generando para intent={intent}, props={len(properties)}")

    if intent == "BUSCAR_MAS" and properties:
        # Limpia props (usa tu clean_description, pero cap a 50 chars)
        cleaned_props = []
        for p in properties[:3]:
            clean_desc = clean_description(p.get('descripcion', ''), p.get('dormitorios', 0), comuna)[:50] + '...'
            amenities_str = ', '.join(p.get('amenities', [])) or 'comodidades básicas'
            cleaned_prop = {
                'codigo': p.get('codigo'),
                'precio_uf': format_uf_price(p.get('precio_uf')),
                'dorms': p.get('dormitorios'),
                'banos': p.get('banos'),
                'desc': clean_desc,
                'amens': amenities_str,
                'link': f"https://www.procasa.cl/{p.get('codigo')}"
            }
            cleaned_props.append(cleaned_prop)

        # USER_PROMPT GUIADO: Esqueleto natural + relleno verbatim (reforzado para full cierre)
        prop_skeleton = "\n".join([
            f"Prop {i+1}: Integra '{cleaned_prop['desc']}' con '{cleaned_prop['amens']}' en frase fluida natural (ej. 'una casa luminosa dorms y piscina para relax'). Precio: {cleaned_prop['precio_uf']} UF. Link: {cleaned_prop['link']}"
            for i, cleaned_prop in enumerate(cleaned_props)
        ])
        
        urgency_adjetivo = 'Genial' if urgency == 'media' else '¡Rápido!'
        prefs_str = ', '.join(criteria.get('prefs_semánticas', [])) or 'tus gustos'
        
        user_prompt = f"""
        Basado en mensaje '{message}', genera respuesta natural y engaging USANDO este esqueleto para {len(cleaned_props)} props. Rellena con flow conversacional, pero COPIA verbatim desc/amens SIN agregar extras.

        Esqueleto OBLIGATORIO (full, sin cutoff):
        Intro: '{urgency_adjetivo}! Para {summary_criteria} en {comuna} con {prefs_str}, te recomiendo estas que suenan perfectas 😊:'
        {prop_skeleton}
        Cierre EXACTO: '¿Cuál te tinca más? Te conecto con asesor para agendar visita. ¡O dime si quieres más opciones o agregar características especificas!'

        Mantén cálido: Usa 'me encanta esta porque...' si encaja natural, PERO verifica contra props. NO inventes ubicaciones/beneficios ajenos. ASEGURA TODOS links y cierre full.
        """

        try:
            resp_text = call_grok(user_prompt, system_prompt=system_prompt, max_tokens=350)  # ↑ para evitar trunc
        except Exception as e:
            print(f"[ERROR] {e}. Fallback natural.")
            # FALLBACK NATURAL (template con toques cálidos)
            intro = f"{urgency_adjetivo}! Para {summary_criteria} en {comuna} {'con piscina' if 'piscina' in str(criteria) else ''}, mira estas opciones que encajan bien 😊:\n"
            prop_lines = [f"• Código {p['codigo']}: {p['precio_uf']} UF, {p['dorms']} dorms/{p['banos']} baños. {p['desc']} Con {p['amens']}. [Ver aquí]({p['link']})." for p in cleaned_props]
            cta = "\n\n¿Cuál te tinca más? Te conecto con asesor para agendar visita o afinar (precio/dorms). ¡O dime si quieres más opciones o agregar características como piscina!"
            resp_text = intro + "\n".join(prop_lines) + cta

        # NUEVO: Chequeo anti-trunc (cuenta links/prop lines)
        link_count = len(re.findall(r'https://www\.procasa\.cl/\d+', resp_text))
        if link_count < len(cleaned_props):
            print(f"[WARN] Trunc detectada ({link_count}/{len(cleaned_props)} links). Fallback full.")
            # Regenera con template
            intro = f"{urgency_adjetivo}! Para {summary_criteria} en {comuna} {'con piscina' if 'piscina' in str(criteria) else ''}, mira estas opciones que encajan bien 😊:\n"
            prop_lines = [f"• Código {p['codigo']}: {p['precio_uf']} UF, {p['dorms']} dorms/{p['banos']} baños. {p['desc']} Con {p['amens']}. Chequéala en: {p['link']}." for p in cleaned_props]
            cta = "\n\n¿Cuál te tinca más? Te conecto con asesor para agendar visita o afinar (precio/dorms). ¡O dime si quieres más opciones o agregar características como piscina!"
            resp_text = intro + "\n".join(prop_lines) + cta

    else:
        # Para otros intents: Prompt simple natural
        user_prompt = f"Responde cálidamente al '{message}', usando criteria {json.dumps(criteria)}. Si necesita_mas_info, pregunta fluido (ej. '¿Me das más de dorms o precio para afinar?'). Mantén engaging."
        try:
            resp_text = call_grok(user_prompt, system_prompt=system_prompt, max_tokens=150)
        except Exception as e:
            print(f"[ERROR] {e}. Fallback simple.")
            resp_text = f"¡Hola! Cuéntame más de tu búsqueda para ayudarte perfecto 😊."

    # === POST-PROCESO SUAVE: Strip solo alucs obvias ===
    # Lista expandida (agrega 'colón' de logs)
    aluc_keywords = ['mall dominicos', 'verbo divino', 'disfrutar en familia', 'refrescar el día', 'colón']  # De logs
    for kw in aluc_keywords:
        if kw in resp_text.lower() and kw not in ' '.join([p.get('desc', '') + p.get('amens', '') for p in cleaned_props]):
            resp_text = resp_text.replace(kw, '')  # Strip si no en props
            print(f"[LOG] Stripped aluc: {kw}")

    # Regex para formato links (mantén)
    resp_text = clean_markdown_links(resp_text)

    # Si demasiado corto/rígido, inyecta calidez post (opcional)
    if len(resp_text) < 100:
        resp_text += " ¡Suena como una búsqueda emocionante, estoy aquí para hacerla fácil! 😊"

    print(f"[LOG] Respuesta natural-anti-aluc: {resp_text[:120]}...")
    
    # FIX GRAVE: Si necesita_mas_info=True, fuerza fallback que SOLO pregunta por cores, SIN props ni recomendaciones
    if criteria.get('necesita_mas_info', False):
        missing_cores = []
        core_keys = config.CORE_KEYS  # Dinámico
        for k in core_keys:
            if not criteria.get(k):
                if k == 'operacion':
                    missing_cores.append('venta o arriendo')
                elif k == 'tipo':
                    missing_cores.append('casa, depto u otro')
                elif k == 'comuna':
                    missing_cores.append('comuna')
        missing_str = ' y '.join(missing_cores) if missing_cores else 'más detalles generales'
        
        # NUEVO: Chequea partials (features no-cores) para personalizar
        partial_keys = [k for k in ['tipo', 'precio_uf', 'dormitorios', 'banos'] if criteria.get(k)]
        if len(partial_keys) > 1:
            partial_summary = []
            if criteria.get('tipo'):
                partial_summary.append(criteria['tipo'])
            if criteria.get('precio_uf'):
                val = list(criteria['precio_uf'].values())[0]
                partial_summary.append(f"hasta {val} UF")
            if criteria.get('dormitorios'):
                partial_summary.append(f"{list(criteria['dormitorios'].values())[0]} dorms")
            intro = f"¡Genial, veo que buscas {', '.join(partial_summary)}! "
        else:
            intro = "¡Genial!" if not partial_keys else f"¡Perfecto, ya noto {criteria.get(partial_keys[0], '')}! "
        
        # Mejora: Si algunos cores OK, menciona
        known_cores = [k for k in core_keys if criteria.get(k)]
        if known_cores:
            intro += f"Ya tengo {', '.join([criteria[k] for k in known_cores])} en mente. "
        
        # NUEVO: En feedback_mode, suaviza el fallback SOLO si early (no si ya hay known_cores)
        if feedback_mode and len(known_cores) == 0:  # Fade si ya avanzó
            intro = "¡Hola de nuevo! 😊 Recordamos tu interés reciente en propiedades. "
        else:
            intro = "¡Hola! 😊 Soy de Procasa..." if "hola" not in intro.lower() else intro  # Genérico
        
        # Arma resp_text con guard
        resp_text = f"{intro}Para buscar opciones que te encajen, necesito saber {missing_str}. ¿Me cuentas? 😊"
        
        # NUEVO: Log para debug
        print(f"[LOG] Fallback armado: intro='{intro[:50]}...', missing='{missing_str}', full='{resp_text[:100]}...'")
        
        # Limpia post-armado
        resp_text = clean_markdown_links(resp_text)
        
        # NUEVO: Guard post-clean si vacío
        if not resp_text or resp_text.strip() == "":
            resp_text = f"{intro}Cuéntame: ¿venta o arriendo? ¿casa o depto? ¿en qué comuna? 😊"
            print(f"[WARN] clean_markdown_links limpió todo; usando super-default.")
        
        print(f"[LOG] Fallback final por necesita_mas_info=True: '{resp_text[:100]}...'")

    # FIX: Si escalar_humano, pero chequea prefs nuevas → muestra props + cierre suave
    elif criteria.get('escalar_humano', False):
        # Usa vars ya calculadas para dinámico (no hardcode)
        tipo_ex = criteria.get('tipo', 'propiedad')
        comuna_ex = criteria.get('comuna', 'tu zona')
        operacion_ex = criteria.get('operacion', 'tu búsqueda')  # e.g., 'venta' o 'arriendo'
        prefs_nuevas = criteria.get('prefs_semánticas', [])
        if isinstance(prefs_nuevas, list):
            prefs_str = ', '.join(prefs_nuevas) if prefs_nuevas else 'tus preferencias'
        else:
            prefs_str = prefs_nuevas if prefs_nuevas else 'tus preferencias'
        
        if len(prefs_str) > 0 and isinstance(prefs_nuevas, list) and len(prefs_nuevas) > 0 and properties:  # Si prefs y props encontrados
            # Arma respuesta mixta: Props breves + escalada DINÁMICA
            prop_parts = []
            for p in properties[:2]:  # Solo 2 para no alargar
                highlight = p.get('descripcion', '')[:100] + '...'
                prop_parts.append(f"Código {p['codigo']}: {p['precio_uf']} UF, {p['dormitorios']} dorms/{p['banos']} baños. ¡Highlight: {highlight}! Más info: https://www.procasa.cl/{p['codigo']}")
            resp_text = f"¡Genial, incorporé '{prefs_str}'! Aquí 2 opciones que encajan: \n" + "\n".join(prop_parts) + f"\n\nYa escalé tus detalles completos a un asesor de Procasa ({tipo_ex} {operacion_ex} en {comuna_ex} con {prefs_str}). Te contactan pronto. ¡Que tengas un gran día! 😊"
            print(f"[LOG] Respuesta mixta: Props con prefs + escalada suave (dinámica: {tipo_ex} {operacion_ex} en {comuna_ex}).")
        else:
            # Cierre original si no hay nada nuevo (ya dinámico con vars)
            resp_text = f"¡Perfecto! Ya envié todos tus detalles (incluyendo preferencias como {tipo_ex} {operacion_ex} en {comuna_ex}) a un asesor de Procasa. Te contactarán a la brevedad para coordinar. ¡Gracias por tu interés, que tengas un gran día! 😊"
            print(f"[LOG] Fallback forzado por escalar_humano=True: Cierre cortés.")
        
    # FIX anterior: Fallback solo si 0 props y necesita_mas_info=False y NO escalar_humano
    elif intent == "BUSCAR_MAS" and len(properties) == 0 and not criteria.get('necesita_mas_info', False) and not criteria.get('escalar_humano', False):
        dorms_ex = criteria.get('dormitorios', 'dormitorios')
        if isinstance(dorms_ex, dict):
            op = list(dorms_ex.keys())[0]
            val = dorms_ex[op]
            if op == '$eq':
                dorms_ex = f"{val} dormitorios"
            elif op == '$gte':
                dorms_ex = f"al menos {val} dormitorios"
            elif op == '$lte':
                dorms_ex = f"máximo {val} dormitorios"
            else:
                dorms_ex = f"{val} dormitorios (condición: {op})"
        else:
            dorms_ex = f"{dorms_ex} dormitorios" if dorms_ex != 'dormitorios' else 'dormitorios'
        
        prefs_ex = criteria.get('prefs_semánticas', '')
        comuna_ex = criteria.get('comuna', 'área')
        tipo_ex = criteria.get('tipo', 'propiedad')
        operacion_ex = criteria.get('operacion', '')
        
        # Agrega precio al fallback si presente
        precio_ex = ""
        precio_uf = criteria.get('precio_uf')
        precio_clp = criteria.get('precio_clp')
        if precio_uf:
            op = list(precio_uf.keys())[0] if isinstance(precio_uf, dict) else ''
            val = list(precio_uf.values())[0] if isinstance(precio_uf, dict) else precio_uf
            if op == '$gte':
                precio_ex += f", desde {val} UF"
            elif op == '$lte':
                precio_ex += f", hasta {val} UF"
        if precio_clp:
            op = list(precio_clp.keys())[0] if isinstance(precio_clp, dict) else ''
            val = list(precio_clp.values())[0] if isinstance(precio_clp, dict) else precio_clp
            if op == '$gte':
                precio_ex += f", desde ${val:,} CLP" if isinstance(val, (int, float)) else f", desde {val} CLP"
            elif op == '$lte':
                precio_ex += f", hasta ${val:,} CLP" if isinstance(val, (int, float)) else f", hasta {val} CLP"
        
        resp_text = f"No encontré exacto con tus criterios (ej. {tipo_ex} {operacion_ex} en {comuna_ex}, {dorms_ex}{precio_ex} {prefs_ex}). ¿Ampliamos el rango de {precio_ex} o agregamos features como baños? ¡Dime!"
        print(f"[LOG] Fallback response dinámico: {resp_text[:50]}...")
    
    print(f"[LOG] Respuesta generada: {resp_text[:120]}...")
    return resp_text

# === NUEVO: WRAPPER OPTIMIZADO PARA AHORRO DE RECURSOS ===
def process_message(history: list, message: str, phone: str) -> tuple[str, dict]:
    """
    Wrapper optimizado: Ramifica basado en sentiment/intent para ahorrar llamadas.
    Returns: (response_text, updated_criteria)
    Nota: Asume 'properties' viene de una DB/search externa; aquí simulado como [] para testing.
    NUEVO: Integra feedback_mode para boostear intents y pasar a generate_response.
    """
    # Paso 1: Siempre sentiment (barato, detecta rechazos rápidos)
    sentiment = analyze_sentiment(message)
    
    if sentiment == "NEGATIVE":
        # Flujo NEGATIVO: Solo intent para confirmar RECHAZO, luego cierre directo (sin urgency/extraction/response completa)
        intent = classify_intent(history, message)
        if intent in ["RECHAZO", "CIERRE_GRACIAS", "EN_PROCESO"]:
            response = "Entendido, gracias por tu tiempo. Si cambias de idea, aquí estoy para ayudarte con propiedades en Procasa 😊. ¡Que tengas un buen día!"
            print(f"[LOG] Flujo NEGATIVO: Respuesta directa para {intent}. Ahorro: 3 llamadas.")
            return response, {}  # No actualiza criteria
        # Si negative pero no rechazo (e.g., frustración refinando), cae al flujo normal
        print(f"[LOG] NEGATIVE pero no rechazo: Procede a flujo completo.")
    
    # Flujo NORMAL/POSITIVO: Intent + Urgency (urgency opcional si no es alta urgencia histórica)
    intent = classify_intent(history, message)
    
    # NUEVO: Boostea intent para greetings en feedback_mode
    feedback_mode = is_feedback_mode(history)
    print(f"[DEBUG] Feedback_mode detectado: {feedback_mode}")  # ← TEMPORAL para tu log
    if feedback_mode:
        greeting_keywords = ["hola", "buenas", "hey", "saludos", "puedes ser", "alo", "hola"]  # Expande para casuales
        if intent == "OTRO" and any(kw in message.lower() for kw in greeting_keywords):
            intent = "FEEDBACK_VISITA" if any(word in message.lower() for word in ["visita", "conocerla", "vi", "fue"]) else "BUSCAR_MAS"
            print(f"[LOG] Intent boosteado a '{intent}' por greeting en feedback_mode.")
    
    urgency = "media"  # Default
    if intent not in ["RECHAZO", "CIERRE_GRACIAS", "OTRO"] and any(word in message.lower() for word in ["urgente", "ya", "mañana"]):  # Chequeo local simple
        urgency = classify_urgency(history, message)  # Solo si probable alta
    else:
        print(f"[LOG] Urgency skipped por low-prob: Default 'media'. Ahorro: 1 llamada en ~70% casos.")
    
    # Extraction solo si no es cierre/otro
    criteria = {}
    if intent in ["BUSCAR_MAS", "AGENDAR_VISITA", "CORRECCION", "VENDER_MI_CASA", "SOLICITUD_CONTACTO"]:
        criteria = extract_criteria(history, message, phone)
    
    is_first_message = len(history) == 0  # O usa len([m for m in history if m["role"] != "assistant"]) == 0 si history incluye assistant inicial
    
    # Response solo si no rechazo
    if intent in ["RECHAZO", "CIERRE_GRACIAS"]:
        response = "¡Gracias por chatear! Si necesitas algo más, avísame. 😊"
    else:
        # Aquí asume que tienes 'properties' de una DB/search externa; simulo vacío para ejemplo
        properties = []  # Reemplaza con tu búsqueda real basada en criteria
        response = generate_response(criteria, history, message, properties, [], intent=intent, urgency=urgency, feedback_mode=feedback_mode, is_first_message=is_first_message)  # ← Pasa is_first_message si aplica
    
    print(f"[LOG] Flujo completado: {intent}/{sentiment}, llamadas: {5 if intent not in ['RECHAZO'] else 2}")
    return response, criteria