# chatbot/rag.py
import logging
import re
import time
from typing import List, Dict, Optional
from config import Config

from .storage import get_db
from .utils import safe_int_conversion
from .semantic_engine import generate_embedding
from .geo_utils import get_neighboring_communes
from .property_lookup import PROPERTY_COLLECTION_NAME

import unicodedata
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

logger = logging.getLogger(__name__)

def remove_accents(input_str):
    if not input_str:
        return ""
    nfkd_form = unicodedata.normalize('NFKD', input_str)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])

def get_accent_regex(text):
    if not text: return ""
    base = remove_accents(text)
    replacements = {
        'a': '[aá]', 'e': '[eé]', 'i': '[ií]', 'o': '[oó]', 'u': '[uúü]',
        'n': '[nñ]',
        'A': '[AÁ]', 'E': '[EÉ]', 'I': '[IÍ]', 'O': '[OÓ]', 'U': '[UÚÜ]',
        'N': '[NÑ]'
    }
    regex_pattern = ""
    for char in base:
        regex_pattern += replacements.get(char, char)
    return regex_pattern

MAP_OPERACION = {
    "venta": "Venta", "comprar": "Venta", "compra": "Venta", "vendo": "Venta",
    "arriendo": "Arriendo", "arrendar": "Arriendo", "alquiler": "Arriendo", "busco arriendo": "Arriendo"
}

MAP_TIPO = {
    "casa": "Casa", "casas": "Casa",
    "depto": "Departamento", "departamento": "Departamento", "flat": "Departamento", "depa": "Departamento",
    "oficina": "Oficina", 
    "local": "Local Comercial", 
    "sitio": "Sitio", "terreno": "Sitio", "parcela": "Parcela"
}

# Mejora Profesional: Cache de comunas normalizadas para detección rápida y robusta
from .geo_utils import NEIGHBOR_MAP_RAW
NORMALIZED_COMMUNES = {
    remove_accents(c.lower()): c
    for c in NEIGHBOR_MAP_RAW.keys()
}

def normalizar_criterio(key: str, valor: str) -> Optional[str]:
    if not valor: return None
    valor = str(valor).lower().strip()
    
    if key == "operacion":
        return MAP_OPERACION.get(valor, valor.title()) 
    if key == "tipo":
        for k, v in MAP_TIPO.items():
            if k in valor:
                return v
        return valor.title()
    return valor

def construir_query(criterios: Dict) -> Dict:
    clauses = []
    
    # 1. Operación
    op = normalizar_criterio("operacion", criterios.get("operacion"))
    if op:
        clauses.append({"$or": [
            {"operacion": op},
            {"tipo_operacion.venta": True if op == "Venta" else False},
            {"tipo_operacion.arriendo": True if op == "Arriendo" else False},
        ]})

    # 2. Tipo de propiedad
    tipo = normalizar_criterio("tipo", criterios.get("tipo"))
    if tipo:
        clauses.append({"$or": [
            {"tipo": tipo},
            {"tipo_operacion.tipo": tipo},
        ]})

    # 3. Comuna (Multi-comuna)
    comuna = criterios.get("comuna")
    if comuna:
        if "," in comuna:
            comunas_lista = [c.strip() for c in comuna.split(",") if c.strip()]
            if comunas_lista:
                clauses.append({"$or": [
                    {"comuna": {"$in": [re.compile(get_accent_regex(c), re.IGNORECASE) for c in comunas_lista]}},
                    {"ubicacion.comuna": {"$in": [re.compile(get_accent_regex(c), re.IGNORECASE) for c in comunas_lista]}},
                ]})
        else:
            comuna_regex = {"$regex": get_accent_regex(comuna), "$options": "i"}
            clauses.append({"$or": [
                {"comuna": comuna_regex},
                {"ubicacion.comuna": comuna_regex},
            ]})

    # 4. Precio (Rango inteligente)
    presupuesto = safe_int_conversion(criterios.get("presupuesto"))
    if presupuesto > 0:
        if presupuesto < 30000:
            clauses.append({"$or": [
                {"precio_uf": {"$lte": presupuesto * 1.15}},
                {"tipo_operacion.precio_venta.precio_uf": {"$lte": presupuesto * 1.15}},
                {"tipo_operacion.precio_arriendo.precio_uf": {"$lte": presupuesto * 1.15}},
            ]})
        else:
            clauses.append({"$or": [
                {"precio_clp": {"$lte": presupuesto * 1.15}},
                {"tipo_operacion.precio_venta.precio_clp": {"$lte": presupuesto * 1.15}},
                {"tipo_operacion.precio_arriendo.precio_clp": {"$lte": presupuesto * 1.15}},
            ]})

    # 5. Dormitorios (mínimo)
    dorms = safe_int_conversion(criterios.get("dormitorios"))
    if dorms > 0:
        clauses.append({"$or": [
            {"dormitorios": {"$gte": dorms}},
            {"caracteristicas.dormitorios": {"$gte": dorms}},
        ]})

    # 6. Baños (mínimo)
    banos = safe_int_conversion(criterios.get("banos"))
    if banos > 0:
        clauses.append({"$or": [
            {"banos": {"$gte": banos}},
            {"caracteristicas.banos": {"$gte": banos}},
        ]})
        
    # 7. Estacionamientos (mínimo)
    estacionamientos = safe_int_conversion(criterios.get("estacionamientos"))
    if estacionamientos > 0:
        clauses.append({"$or": [
            {"estacionamientos": {"$gte": estacionamientos}},
            {"caracteristicas.estacionamientos": {"$gte": estacionamientos}},
        ]})

    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}

def buscar_propiedades(criterios: Dict, exclude_codes: List[str] = None, limit: int = 3) -> List[Dict]:
    """
    Ejecuta la búsqueda en MongoDB 'universo_cartera'.
    exclude_codes: Lista de códigos a NO mostrar porque ya se vieron.
    limit: Máximo estricto (default 3).
    """
    db = get_db()
    collection = db[PROPERTY_COLLECTION_NAME]
    
    query = construir_query(criterios)
    
    # AGREGADO: Exclusión de propiedades ya vistas
    if exclude_codes:
        query = {"$and": [query, {"codigo": {"$nin": exclude_codes}}]} if query else {"codigo": {"$nin": exclude_codes}}

    if not query:
        return []

    logger.info(f"[RAG] Query: {query} | Excluyendo: {len(exclude_codes or [])} props")
    
    projection = {
        "_id": 0, "codigo": 1, "operacion": 1, "tipo": 1, "comuna": 1, 
        "precio_uf": 1, "precio_clp": 1, "dormitorios": 1, "banos": 1, 
        "m2_utiles": 1, "descripcion_clean": 1, "nombre_calle": 1,
        "amenities": 1 # Traemos amenities para el prompt natural
    }

    try:
        # Ordenamos por precio ascendente por defecto
        cursor = collection.find(query, projection).sort([("tipo_operacion.precio_venta.precio_uf", 1), ("precio_uf", 1)]).limit(limit)
        resultados = list(cursor)
        return resultados
    except Exception as e:
        logger.error(f"[RAG] Error en búsqueda: {e}")
        return []

def formatear_resultados_texto(propiedades: List[Dict]) -> str:
    """Convierte resultados JSON a texto para que el LLM los transforme a lenguaje natural."""
    if not propiedades:
        return ""

    texto = "--- INICIO LISTADO PROPIEDADES ENCONTRADAS (RAG) ---\n"
    for p in propiedades:
        texto += (
            f"- Código: {p.get('codigo')}\n"
            f"  Tipo: {p.get('tipo')} en {p.get('operacion')}\n"
            f"  Comuna: {p.get('comuna')}\n"
            f"  Precio: UF {p.get('precio_uf')} (aprox CLP {p.get('precio_clp')})\n"
            f"  Programa: {p.get('dormitorios')} dorms, {p.get('banos')} baños\n"
            f"  Superficie: {p.get('m2_utiles')} m2 útiles\n"
            f"  Amenities/Desc: {str(p.get('descripcion_clean', ''))[:250]}...\n"
            f"  Link: https://www.procasa.cl/{p.get('codigo')}\n\n"
        )
    texto += "--- FIN LISTADO ---"
    return texto


# =============================================================================
# BÚSQUEDA HÍBRIDA (Structured Filters + Semantic Ranking)
# =============================================================================

# Patrones regex para extraer filtros duros del texto libre
_RE_DORMS = re.compile(r'(\d)\s*(?:dormitorio|dorm|pieza|habitaci[oó]n)', re.IGNORECASE)
_RE_BANOS = re.compile(r'(\d)\s*(?:ba[ñn]o)', re.IGNORECASE)
_RE_ESTAC = re.compile(r'(\d)\s*(?:estacionamiento|parking|cochera|estac)', re.IGNORECASE)
_RE_M2 = re.compile(r'(\d{2,4})\s*(?:m2|metros?\s*cuadrados?|mt2)', re.IGNORECASE)
_RE_PRECIO_UF = re.compile(r'(\d[\d.]*)\s*(?:a|y|y\s*hasta|-)?\s*(\d[\d.]*)?\s*(?:uf|UF)', re.IGNORECASE)
_RE_PRECIO_CLP = re.compile(r'(\d[\d.]*)\s*(?:a|y|y\s*hasta|-)?\s*(\d[\d.]*)?\s*(?:millones?|MM|pesos)', re.IGNORECASE)

def needs_semantic(text: str) -> bool:
    """Retorna True si el texto contiene palabras clave que requieren búsqueda semántica."""
    keywords = [
        "vista", "luminos", "piscina", "terraza", "modern", "metro", 
        "seguridad", "remodelad", "patio", "jardin", "quincho", "parque",
        "silencioso", "ruido", "soleado", "despejada", "norte", "sur", "oriente", "poniente"
    ]
    q = text.lower()
    return any(k in q for k in keywords) or len(q.split()) > 6

def extraer_filtros_estructurados(texto: str) -> Dict:
    """
    Extrae filtros estructurados de un texto libre en español.
    Ejemplo: "casa 3 dormitorios en Las Condes arriendo 50 UF"
    -> {operacion: Arriendo, tipo: Casa, dormitorios: 3, comuna: Las Condes, precio_uf: 60}
    """
    filtros = {}
    target_commune = None
    q = texto.lower()

    # 1. Operación
    if any(w in q for w in ["arriend", "alquil", "renta"]):
        filtros["operacion"] = "Arriendo"
    elif any(w in q for w in ["vent", "compr", "compra"]):
        filtros["operacion"] = "Venta"

    # 2. Tipo de propiedad
    for keyword, tipo_val in MAP_TIPO.items():
        if keyword in q:
            filtros["tipo"] = tipo_val
            break

    # 3. Comuna (Detección Robusta con Normalización y Word Boundaries)
    target_communes = []
    q_norm = remove_accents(texto.lower())
    
    # Ordenar por largo desc para matchear "Estación Central" antes que "Central"
    for comuna_norm, comuna_original in sorted(NORMALIZED_COMMUNES.items(), key=lambda x: len(x[0]), reverse=True):
        if re.search(rf'\b{re.escape(comuna_norm)}\b', q_norm):
            target_communes.append(comuna_original)
    
    if target_communes:
        filtros["comunas"] = target_communes
        target_commune = target_communes[0] # Primaria para boost

    # 4. Dormitorios
    m = _RE_DORMS.search(texto)
    if m:
        filtros["dormitorios"] = int(m.group(1))

    # 5. Baños
    m = _RE_BANOS.search(texto)
    if m:
        filtros["banos"] = int(m.group(1))

    # 5b. Estacionamientos
    m = _RE_ESTAC.search(texto)
    if m:
        filtros["estacionamientos"] = int(m.group(1))

    # 6. Superficie mínima
    m = _RE_M2.search(texto)
    if m:
        filtros["m2_utiles"] = int(m.group(1))

    # 7. Precio (UF o CLP) - Detectar RANGOS
    m_uf = _RE_PRECIO_UF.search(texto)
    if m_uf:
        val1 = float(m_uf.group(1).replace(".", "").replace(",", "."))
        val2 = m_uf.group(2)
        if val2:
            val2 = float(val2.replace(".", "").replace(",", "."))
            # Ordenar por si acaso puso "entre 50 y 30"
            filtros["precio_uf_min"] = min(val1, val2)
            filtros["precio_uf_max"] = max(val1, val2)
        else:
            # Una sola cifra: min o max según contexto
            if any(w in q for w in ["desde", "minimo", "min", "superior"]):
                filtros["precio_uf_min"] = val1
            else:
                filtros["precio_uf_max"] = val1
    else:
        m_clp = _RE_PRECIO_CLP.search(texto)
        if m_clp:
            val1 = float(m_clp.group(1).replace(".", "").replace(",", "."))
            if val1 < 1000: val1 *= 1_000_000
            
            val2 = m_clp.group(2)
            if val2:
                val2 = float(val2.replace(".", "").replace(",", "."))
                if val2 < 1000: val2 *= 1_000_000
                filtros["precio_clp_min"] = min(val1, val2)
                filtros["precio_clp_max"] = max(val1, val2)
            else:
                if any(w in q for w in ["desde", "minimo", "min", "superior"]):
                    filtros["precio_clp_min"] = val1
                else:
                    filtros["precio_clp_max"] = val1

    return filtros, target_commune


def _construir_filtros_mongo(filtros: Dict, comunas: List[str] = None, oficina: str = None) -> Dict:
    """Convierte los filtros extraídos en un query MongoDB."""
    query = {"vector_descripcion": {"$exists": True}}
    
    # Filtro de oficina obligatorio
    if oficina:
        query["oficina"] = oficina
    
    if filtros.get("operacion"):
        query["operacion"] = filtros["operacion"]
    
    if filtros.get("tipo"):
        query["tipo"] = filtros["tipo"]
    
    if comunas:
        # Usamos regex robusta para encontrar Maipu/Maipú
        regex_list = []
        for c in comunas:
            pattern = get_accent_regex(c)
            regex_list.append(re.compile(pattern, re.IGNORECASE))
        query["comuna"] = {"$in": regex_list}
    
    if filtros.get("dormitorios"):
        d_val = filtros["dormitorios"]
        # Soporte para int y string: generamos lista ["3", "4", "5", ...] para emular $gte
        possible_dorms = [str(i) for i in range(d_val, 15)] + [i for i in range(d_val, 15)]
        query["dormitorios"] = {"$in": possible_dorms}
    
    if filtros.get("banos"):
        b_val = filtros["banos"]
        possible_banos = [str(i) for i in range(b_val, 15)] + [i for i in range(b_val, 15)]
        query["banos"] = {"$in": possible_banos}
        
    if filtros.get("estacionamientos"):
        e_val = filtros["estacionamientos"]
        possible_estac = [str(i) for i in range(e_val, 15)] + [i for i in range(e_val, 15)]
        query["estacionamientos"] = {"$in": possible_estac}
    
    if filtros.get("m2_utiles"):
        # Superficie es más complejo, pero si suele ser int lo dejamos como $gte.
        # Si falla, m2_utiles suele ser numérico en la mayoría de DBs.
        query["m2_utiles"] = {"$gte": filtros["m2_utiles"]}
    
    # Precios (Rango no excluyente) - SOLO SI FUERON MENCIONADOS
    if filtros.get("precio_uf_max") or filtros.get("precio_uf_min"):
        query["precio_uf"] = {}
        if filtros.get("precio_uf_max"): query["precio_uf"]["$lte"] = filtros["precio_uf_max"]
        if filtros.get("precio_uf_min"): query["precio_uf"]["$gte"] = filtros["precio_uf_min"]

    if filtros.get("precio_clp_max") or filtros.get("precio_clp_min"):
        query["precio_clp"] = {}
        if filtros.get("precio_clp_max"): query["precio_clp"]["$lte"] = filtros["precio_clp_max"]
        if filtros.get("precio_clp_min"): query["precio_clp"]["$gte"] = filtros["precio_clp_min"]
    
    # CRITICAL: Solamente propiedades disponibles
    query["disponible"] = True
    
    return query


def buscar_semanticamente(query_text: str, limit: int = 3, 
                          oficina_filtro: str = "INMOBILIARIA SUCRE SPA",
                          exclude_codes: list = None,
                          include_neighbors: bool = False) -> List[Dict]:
    """
    BÚSQUEDA HÍBRIDA:
    1) Extrae filtros duros del texto (tipo, operación, dormitorios, precio, comuna...)
    2) Filtra MongoDB con esos campos estructurados (rápido y preciso)
    3) Rankea los resultados filtrados por similaridad semántica (piscina, sol, metro...)
    4) Fallback geográfico a comunas vecinas si hay pocos resultados
    """
    db = get_db()
    collection = db[Config.COLLECTION_NAME]

    # --- Paso 1: Extraer filtros estructurados ---
    filtros, target_commune = extraer_filtros_estructurados(query_text)
    
    # Support multiple communes from extraction
    extracted_communes = filtros.get("comunas", [])
    if target_commune and target_commune not in extracted_communes:
        extracted_communes.insert(0, target_commune)
    
    logger.info(f"[RAG-HYBRID] Filtros extraidos: {filtros} | Comunas: {extracted_communes}")

    # --- Paso 2: Generar vector del query (Solo si es necesario) ---
    use_semantic = needs_semantic(query_text)
    query_vector = None
    if use_semantic:
        logger.info("[RAG-HYBRID] Generando embedding para búsqueda semántica...")
        query_vector = generate_embedding(query_text)
    else:
        logger.info("[RAG-HYBRID] Saltando embedding (Búsqueda por filtros estructurados)")

    # --- Paso 3: Helper de búsqueda vectorial con filtros ---
    excluded = set(exclude_codes or [])

    def ejecutar_busqueda(comunas: List[str] = None, relajar_filtros: bool = False, global_scope: bool = False):
        nonlocal use_semantic
        target_office = None if global_scope else oficina_filtro
        if relajar_filtros:
            # RELAXED: Remove M2 but KEEP PRICE and BEDROOMS (User requested strict bedrooms)
            filtros_relajados = {k: v for k, v in filtros.items() 
                               if k in ["operacion", "tipo", "precio_uf_max", "precio_uf_min", "precio_clp_max", "precio_clp_min", "dormitorios"]}
            mongo_query = _construir_filtros_mongo(filtros_relajados, comunas, target_office)
        else:
            mongo_query = _construir_filtros_mongo(filtros, comunas, target_office)

        projection = {
            "vector_descripcion": 1, "codigo": 1, "oficina": 1,
            "comuna": 1, "operacion": 1, "tipo": 1, "precio_uf": 1, "precio_clp": 1,
            "dormitorios": 1, "banos": 1, "m2_utiles": 1, "descripcion_clean": 1,
            "nombre_calle": 1
        }

        candidatos = list(collection.find(mongo_query, projection).limit(2000))
        if use_semantic:
            vectors = [c.get("vector_descripcion") for c in candidatos if c.get("vector_descripcion")]
            if not vectors:
                logger.warning("[RAG-HYBRID] Sin vectores en candidatos, bajando a ranking por filtros")
                use_semantic = False
                
        # Ranking
        scored = []
        if use_semantic:
            t0 = time.time()
            sims = cosine_similarity([query_vector], vectors)[0]
            logger.info(f"[RAG-HYBRID] Similaridad calculada en {time.time()-t0:.3f}s")
            
            for idx, cand in enumerate(candidatos if len(vectors)==len(candidatos) else [c for c in candidatos if c.get("vector_descripcion")]):
                if cand.get("codigo") in excluded: continue
                score = float(sims[idx])
                # Boost comuna exacta (+15% score)
                if target_commune and cand.get("comuna", "").lower() == target_commune.lower():
                    score += 0.15
                scored.append((score, cand))
        else:
            # Ranking básico por filtros (score base 1.0)
            for cand in candidatos:
                if cand.get("codigo") in excluded: continue
                score = 1.0
                if target_commune and cand.get("comuna", "").lower() == target_commune.lower():
                    score += 0.20 
                scored.append((score, cand))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    # --- Paso 4: Ejecutar búsqueda con filtros estrictos ---
    commune_scope = extracted_communes if extracted_communes else None
    logger.info(f"[RAG-HYBRID] Paso 4: Búsqueda estricta. Scope: {commune_scope}")
    results = ejecutar_busqueda(commune_scope)
    logger.info(f"[RAG-HYBRID] Resultados con filtros estrictos: {len(results)}")

    # Track codes already collected to avoid re-adding from looser searches
    collected_codes = {doc.get("codigo") for _, doc in results}

    # --- Paso 5: Fallback progresivo (solo si hay pocos resultados ÚNICOS) ---
    def _unique_count():
        return len({doc.get("codigo") for _, doc in results})

    neighbors = []
    # INTENT PROTECTION: Si el usuario especificó comuna y no hay resultados locales, 
    # verificamos si existen resultados GLOBALES en esa zona antes de expandir.
    if _unique_count() == 0 and extracted_communes:
        logger.info(f"[RAG-HYBRID] Cero resultados locales para {extracted_communes}. Consultando red global en la misma zona...")
        geo_scope = list(set(extracted_communes)) # Solo la comuna original
        global_zone_results = ejecutar_busqueda(comunas=geo_scope, relajar_filtros=False, global_scope=True)
        if global_zone_results:
            logger.info(f"[RAG-HYBRID] Encontrados {len(global_zone_results)} en la red global para {geo_scope}")
            results.extend(global_zone_results)
            collected_codes.update({doc.get("codigo") for _, doc in global_zone_results})
            results.sort(key=lambda x: x[0], reverse=True)
        elif not include_neighbors:
            logger.info(f"[RAG-HYBRID] Cero resultados en toda la red para {geo_scope}. include_neighbors=False, fin de búsqueda.")
            return [] 

    # 5a. EXPANSIÓN A VECINOS - SOLO SI include_neighbors=True y pocos resultados
    if include_neighbors and _unique_count() < limit and target_commune:
        from .geo_utils import get_neighboring_communes
        neighbors = get_neighboring_communes(target_commune)
        if neighbors:
            logger.info(f"[RAG-HYBRID] include_neighbors=True: Expandiendo a vecinos: {neighbors}")
            expanded = ejecutar_busqueda(neighbors)
            for score, cand in expanded:
                code = cand.get("codigo")
                if code not in collected_codes:
                    # Marcar como expandido
                    cand["expanded_from"] = target_commune
                    results.append((score, cand))
                    collected_codes.add(code)
            results.sort(key=lambda x: x[0], reverse=True)
    if _unique_count() < limit:
        logger.info("[RAG-HYBRID] Relajando filtros (sin dormitorios/precio)...")
        # Include target commune + neighbors for the relaxed search
        geo_scope = list(set((commune_scope or []) + neighbors))
        relaxed = ejecutar_busqueda(geo_scope or None, relajar_filtros=True)
        for item in relaxed:
            code = item[1].get("codigo")
            if code not in collected_codes:
                results.append(item)
                collected_codes.add(code)
        results.sort(key=lambda x: x[0], reverse=True)

    # 5c. GLOBAL FALLBACK - PERO MANTENIENDO COMUNA (Si fue especificada)
    if _unique_count() < limit:
        # Si el usuario NO especificó comuna, el scope geográfico es None (Realmente global)
        # Si el usuario SÍ especificó comuna, seguimos restringidos a esa zona (o vecinos) pero en toda la red
        if extracted_communes:
            geo_scope = list(set(extracted_communes + neighbors))
            logger.info(f"[RAG-HYBRID] Fallback Global con restricción de zona: {geo_scope}")
            global_results = ejecutar_busqueda(comunas=geo_scope, relajar_filtros=True, global_scope=True)
        else:
            logger.info("[RAG-HYBRID] Fallback Global TOTAL (Sin restricción de zona)")
            global_results = ejecutar_busqueda(comunas=None, relajar_filtros=True, global_scope=True)

        for item in global_results:
            code = item[1].get("codigo")
            if code not in collected_codes:
                results.append(item)
                collected_codes.add(code)
        results.sort(key=lambda x: x[0], reverse=True)

    # --- Paso 6: Deduplicar y formatear ---
    final_output = []
    seen_codes = set()

    for score, doc in results:
        codigo = doc.get("codigo")
        if codigo in seen_codes:
            continue
        if len(final_output) >= limit:
            break

        doc.pop("vector_descripcion", None)
        doc.pop("_id", None)
        doc["score"] = round(score, 4)
        final_output.append(doc)
        seen_codes.add(codigo)

    logger.info(f"[RAG-HYBRID] Retornando {len(final_output)} resultados")
    return final_output
