# criteria_extractor.py
import json
import random
from typing import Dict, Any, List, Optional

# ================================
# 1. Normalización ULTRA SEGURA (nunca más crash con None)
# ================================
def normalizar_para_mongo(criteria: Dict[str, Any]) -> Dict[str, Any]:
    norm = {}

    # Operación
    op_raw = criteria.get("operacion") or ""
    op = str(op_raw).lower().strip()
    if any(x in op for x in ["venta", "compr", "adquir", "casa propia"]):
        norm["operacion"] = "Venta"
    elif any(x in op for x in ["arriendo", "arendar", "alquiler", "renta"]):
        norm["operacion"] = "Arriendo"

    # Tipo
    tipo_raw = criteria.get("tipo") or ""
    tipo = str(tipo_raw).lower().strip()
    tipo_map = {
        "casa": "Casa",
        "depto": "Departamento", "departamento": "Departamento", "dpto": "Departamento",
        "oficina": "Oficina",
        "local": "Local Comercial", "comercial": "Local Comercial",
        "bodega": "Bodega",
        "parcela": "Parcela",
        "terreno": "Terreno", "sitio": "Terreno", "solar": "Terreno"
    }
    for clave, valor in tipo_map.items():
        if clave in tipo:
            norm["tipo"] = valor
            break

    # COMUNA → AHORA SOPORTA LISTA!
    comuna_raw = criteria.get("comuna")
    correcciones = {
        "Las Condes": "Las Condes", "las condes": "Las Condes",
        "Lo Barnechea": "Lo Barnechea", "lo barnechea": "Lo Barnechea",
        "Ñuñoa": "Ñuñoa", "Nunoa": "Ñuñoa", "ñuñoa": "Ñuñoa",
        "Providencia": "Providencia",
        "La Reina": "La Reina", "la reina": "La Reina",
        "Vitacura": "Vitacura",
        "Macul": "Macul",
        "Peñalolén": "Peñalolén", "penalolen": "Peñalolén",
        "La Florida": "La Florida",
        "Santiago": "Santiago", "santiago centro": "Santiago"
    }

    if isinstance(comuna_raw, list):
        norm["comuna"] = [correcciones.get(c.strip().title(), c.strip().title()) for c in comuna_raw if c]
    elif comuna_raw:
        c = str(comuna_raw).strip().title()
        norm["comuna"] = correcciones.get(c, c)

    # Resto de campos
    for key in ["precio_uf", "precio_clp", "dormitorios", "banos", "estacionamientos"]:
        if key in criteria and criteria[key] is not None:
            norm[key] = criteria[key]

    return norm

# ================================
# 2. Llamada a Grok
# ================================
def call_grok_extractor(prompt: str) -> Optional[str]:
    try:
        from chatbot import call_grok
        return call_grok(prompt, temperature=0.0)
    except Exception as e:
        print(f"[GROK EXTRACTOR] Error: {e}")
        return None
    
# ================================
# 3. Extracción con Grok
# ================================
def extract_with_grok(user_msg: str, history_summary: str = "") -> Dict[str, Any]:
    prompt = f"""
Eres un extractor ultrarrápido y preciso de criterios inmobiliarios en Chile.
Devuelve SOLO JSON válido, sin texto adicional, sin ```json.

Mensaje del cliente: "{user_msg}"
Contexto anterior: {history_summary[-300:] if history_summary else "Sin contexto"}

Extrae TODOS los criterios que el cliente haya mencionado en cualquier momento.
Los 3 principales (operacion/tipo/comuna) son obligatorios cuando los diga, pero NUNCA ignores los filtros adicionales.

Campos posibles:
- "operacion": "Venta" | "Arriendo" | null
- "tipo": "Casa" | "Departamento" | "Oficina" | "Local Comercial" | "Bodega" | "Parcela" | "Terreno" | null
- "comuna": string o lista de comunas (corrige tildes y mayúsculas) | null
- "dormitorios": número entero (mínimo deseado, ej: "al menos 4" → 4)
- "banos": número entero (mínimo)
- "estacionamientos": número entero (mínimo)
- "precio_uf": {{"$lte": número}} si dice "hasta", "máximo", "no más de"
- "precio_clp": {{"$lte": número}} si menciona millones o pesos chilenos

Ejemplos:
"busco casa en las condes venta al menos 4 dormitorios máximo 18.000 UF" → 
{{"operacion": "Venta", "tipo": "Casa", "comuna": "Las Condes", "dormitorios": 4, "precio_uf": {{"$lte": 18000}}}}

"depto en ñuñoa o providencia arriendo con 3 dorm y 2 estacionamientos" →
{{"operacion": "Arriendo", "tipo": "Departamento", "comuna": ["Ñuñoa", "Providencia"], "dormitorios": 3, "estacionamientos": 2}}

Solo responde el JSON.
"""

    resp = call_grok_extractor(prompt)
    if not resp:
        return {}

    try:
        cleaned = resp.strip().strip("```json").strip("```").strip()
        data = json.loads(cleaned)
        return data
    except Exception as e:
        print(f"[ERROR JSON] No se pudo parsear extractor Grok: {e} | Resp: {resp}")
        return {}

# ================================
# 4. Fallback mínimo si Grok falla totalmente
# ================================
def fallback_basico(user_msg: str) -> Dict[str, Any]:
    lower = user_msg.lower()
    crit = {}
    if any(x in lower for x in ["venta", "compr"]):
        crit["operacion"] = "venta"
    if any(x in lower for x in ["arriendo", "alquiler"]):
        crit["operacion"] = "arriendo"
    if "casa" in lower:
        crit["tipo"] = "casa"
    if "depto" in lower or "departamento" in lower:
        crit["tipo"] = "departamento"
    return crit


# ================================
# 5. FUNCIÓN PRINCIPAL – NUNCA MÁS CRASH
# ================================
def extract_criteria(user_msg: str, history: List[Dict[str, Any]]) -> Dict[str, Any]:
    history_summary = " ".join([m.get("content", "") for m in history[-6:] if m.get("role") == "user"])
    grok_data = extract_with_grok(user_msg, history_summary)
    if grok_data:
        return normalizar_para_mongo(grok_data)
    return normalizar_para_mongo(fallback_basico(user_msg))