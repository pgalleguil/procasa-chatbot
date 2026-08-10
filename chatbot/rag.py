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
        "amenities": 1,
        "tipo_operacion": 1, "ubicacion": 1, "caracteristicas": 1, "observaciones": 1
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

    from .property_lookup import get_prop_location, get_prop_operation
    
    texto = "--- INICIO LISTADO PROPIEDADES ENCONTRADAS (RAG) ---\n"
    for p in propiedades:
        prop_loc = get_prop_location(p)
        prop_op = get_prop_operation(p)
        caract = p.get("caracteristicas") or {}
        obs = p.get("observaciones") or {}
        
        dormitorios = caract.get("dormitorios") or p.get("dormitorios") or "N/D"
        banos = caract.get("banos") or p.get("banos") or "N/D"
        sup_util = caract.get("superficie_util") or p.get("m2_utiles") or "N/D"
        if sup_util == "N/D" or sup_util in (None, ""):
            sup_util = caract.get("superficie_construida") or caract.get("superficie_terreno") or "N/D"
        descripcion = obs.get("descripcion") or p.get("descripcion_clean") or ""
        
        texto += (
            f"- Código: {p.get('codigo')}\n"
            f"  Tipo: {prop_op.get('tipo')} en {prop_op.get('operacion')}\n"
            f"  Comuna: {prop_loc.get('comuna')}\n"
            f"  Precio: {_format_uf_display(prop_op.get('precio_uf'))} (aprox CLP {prop_op.get('precio_clp')})\n"
            f"  Programa: {dormitorios} dorms, {banos} baños\n"
            f"  Superficie: {sup_util} m2 útiles\n"
            f"  Amenities/Desc: {str(descripcion)[:250]}...\n"
            f"  Link: https://www.procasa.cl/{p.get('codigo')}\n\n"
        )
    texto += "--- FIN LISTADO ---"
    return texto

# =======================================================================================================

def _format_uf_display(value) -> str:
    """Formatea un valor UF con notación chilena para texto de resultados."""
    if value is None:
        return "N/D"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    if amount == int(amount):
        return f"{int(amount):,}".replace(",", ".") + " UF"
    s = f"{amount:.2f}".rstrip("0").rstrip(".")
    int_part, _, dec_part = s.partition(".")
    return f"{int(int_part):,}".replace(",", ".") + (f",{dec_part}" if dec_part else "") + " UF"


# Patrones regex para extraer filtros duros del texto libre
_RE_DORMS = re.compile(r'(\d{1,2})\s*(?:dormitorio|dorm|pieza|habitaci[oó]n)', re.IGNORECASE)
_RE_BANOS = re.compile(r'(\d{1,2})\s*(?:ba[ñn]o)', re.IGNORECASE)
_RE_ESTAC = re.compile(r'(\d{1,2})\s*(?:estacionamiento|parking|cochera|estac)', re.IGNORECASE)
_RE_M2 = re.compile(r'(\d{2,4})\s*(?:m2|metros?\s*cuadrados?|mt2)', re.IGNORECASE)

# Números en palabras ("un solo dormitorio", "dos baños") para extracción robusta
_NUMEROS_PALABRA = {
    "un": 1, "una": 1, "uno": 1, "solo": 1, "sola": 1, "único": 1, "unica": 1, "unico": 1,
    "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
    "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
}
_RE_DORMS_PALABRA = re.compile(
    r'\b(un\s+solo|una\s+sola|un|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\s+'
    r'(?:solo|sola)?\s*(?:dormitorio|dorm|pieza|habitaci[oó]n)',
    re.IGNORECASE)
_RE_BANOS_PALABRA = re.compile(
    r'\b(un\s+solo|una\s+sola|un|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\s+'
    r'(?:solo|sola)?\s*(?:ba[ñn]o)',
    re.IGNORECASE)
_RE_PRECIO_UF = re.compile(r'(\d[\d.]*)\s*(?:a|y|y\s*hasta|-)?\s*(\d[\d.]*)?\s*(?:uf|UF)', re.IGNORECASE)
_RE_PRECIO_CLP = re.compile(r'(\d[\d.]*)\s*(?:a|y|y\s*hasta|-)?\s*(\d[\d.]*)?\s*(millones?|MM|mil|pesos)', re.IGNORECASE)

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

    # 4. Dormitorios (exacto si el usuario pide "N dormitorios"; mínimo solo si dice "desde/al menos")
    m = _RE_DORMS.search(texto)
    if not m:
        m = _RE_DORMS_PALABRA.search(texto)
        if m:
            filtros["dormitorios"] = _NUMEROS_PALABRA.get(m.group(1).lower().split()[0], 1)
    else:
        filtros["dormitorios"] = int(m.group(1))
    if filtros.get("dormitorios") and any(w in q for w in ["desde", "al menos", "minimo", "mínimo", "como minimo", "mas de", "más de", "o mas", "o más", "+"]):
        filtros["dormitorios_exacto"] = False
    elif filtros.get("dormitorios"):
        filtros["dormitorios_exacto"] = True

    # 5. Baños
    m = _RE_BANOS.search(texto)
    if not m:
        m = _RE_BANOS_PALABRA.search(texto)
        if m:
            filtros["banos"] = _NUMEROS_PALABRA.get(m.group(1).lower().split()[0], 1)
    else:
        filtros["banos"] = int(m.group(1))
    if filtros.get("banos") and not any(w in q for w in ["desde", "al menos", "minimo", "mínimo", "mas de", "más de", "o mas", "o más", "+"]):
        filtros["banos_exacto"] = True

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
            # Una sola cifra: min o max según contexto. Margen +15% al máximo
            # (mismo criterio que presupuestos del prospecto en _mezclar_criterios).
            if any(w in q for w in ["desde", "minimo", "min", "superior"]):
                filtros["precio_uf_min"] = val1
            else:
                filtros["precio_uf_max"] = val1 * 1.15
    else:
        m_clp = _RE_PRECIO_CLP.search(texto)
        if m_clp:
            unid = (m_clp.group(3) or "millones").lower()
            mult = 1_000_000 if unid.startswith("millo") or unid == "mm" else 1000
            val1 = float(m_clp.group(1).replace(".", "").replace(",", "."))
            if val1 < 1000: val1 *= mult
            
            val2 = m_clp.group(2)
            if val2:
                val2 = float(val2.replace(".", "").replace(",", "."))
                if val2 < 1000: val2 *= mult
                filtros["precio_clp_min"] = min(val1, val2)
                filtros["precio_clp_max"] = max(val1, val2)
            else:
                if any(w in q for w in ["desde", "minimo", "min", "superior"]):
                    filtros["precio_clp_min"] = val1
                else:
                    filtros["precio_clp_max"] = val1 * 1.15

    return filtros, target_commune


def _mezclar_criterios(filtros: Dict, target_commune: str, criterios: Dict) -> tuple[Dict, str]:
    """Combina criterios estructurados del prospecto con los filtros extraídos del texto.
    Los criterios del texto tienen prioridad; los del prospecto rellenan lo que falta."""
    if not criterios:
        return filtros, target_commune

    if not filtros.get("operacion") and criterios.get("operacion"):
        filtros["operacion"] = criterios["operacion"]
    if not filtros.get("tipo") and criterios.get("tipo"):
        filtros["tipo"] = criterios["tipo"]
    if not filtros.get("comunas") and criterios.get("comuna"):
        valor = str(criterios["comuna"]).strip()
        if valor:
            comunas = [c.strip() for c in valor.split(",") if c.strip()]
            if comunas:
                filtros["comunas"] = comunas
                if not target_commune:
                    target_commune = comunas[0]
    if not filtros.get("dormitorios") and criterios.get("dormitorios"):
        filtros["dormitorios"] = criterios["dormitorios"]
    if not filtros.get("banos") and criterios.get("banos"):
        filtros["banos"] = criterios["banos"]
    if not filtros.get("estacionamientos") and criterios.get("estacionamientos"):
        filtros["estacionamientos"] = criterios["estacionamientos"]
    if not (filtros.get("precio_uf_max") or filtros.get("precio_clp_max")
            or filtros.get("precio_uf_min") or filtros.get("precio_clp_min")) and criterios.get("presupuesto"):
        presupuesto = safe_int_conversion(criterios["presupuesto"])
        if presupuesto > 0:
            if presupuesto < 30000:
                filtros["precio_uf_max"] = presupuesto * 1.15
            else:
                filtros["precio_clp_max"] = presupuesto * 1.15
    return filtros, target_commune


# Mapeo de nombres de oficina: caller legacy (CRM) -> esquema Prop360 actual.
OFICINA_MAP = {
    "INMOBILIARIA SUCRE SPA": "PROCASA SUCRE",
    "PROCASA SUCRE": "PROCASA SUCRE",
    "INMOBILIARIA CARLOS HURTADO SPA": "PROCASA CARLOS HURTADO",
    "PROCASA CARLOS HURTADO": "PROCASA CARLOS HURTADO",
    "PROCASA FRANCISCO VIAL": "PROCASA FRANCISCO VIAL",
    "PROCASA GRUPO ORIENTE": "PROCASA GRUPO ORIENTE",
    "PROCASA LA GLORIA": "PROCASA LA GLORIA",
    "PROCASA MAURICIO PINO": "PROCASA MAURICIO PINO",
    "PROCASA VILLARRICA": "PROCASA VILLARRICA",
}

def _normalizar_oficina(value: str) -> str:
    v = (value or "").strip()
    return OFICINA_MAP.get(v, v)


def _superficie_paths(tipo: str) -> List[str]:
    """Campos de superficie relevantes según el tipo de propiedad."""
    t = (tipo or "").upper()
    if any(k in t for k in ("SITIO", "PARCELA", "TERRENO")):
        return ["caracteristicas.superficie_terreno", "caracteristicas.superficie_total", "m2_utiles"]
    if any(k in t for k in ("LOCAL", "OFICINA", "BODEGA", "INDUSTRIAL", "ESTACIONAMIENTO")):
        return ["caracteristicas.superficie_util", "caracteristicas.superficie_construida", "m2_utiles"]
    return ["caracteristicas.superficie_util", "caracteristicas.superficie_construida",
            "caracteristicas.superficie_total", "caracteristicas.superficie_terreno", "m2_utiles"]


def _construir_filtros_mongo(filtros: Dict, comunas: List[str] = None, oficina: str = None, relajar_filtros: bool = False) -> Dict:
    """Convierte los filtros extraídos en un query MongoDB para el esquema
    anidado de universo_cartera_prop360 (mantiene fallbacks planos heredados)."""
    clauses = [{"vector_descripcion": {"$exists": True}}]

    # Filtro de oficina (resumen.oficina / oficina_nombre / estado.oficina / plano)
    if oficina:
        ofi = _normalizar_oficina(oficina)
        if ofi:
            clauses.append({"$or": [
                {"resumen.oficina": {"$regex": re.escape(ofi), "$options": "i"}},
                {"oficina_nombre": {"$regex": re.escape(ofi), "$options": "i"}},
                {"estado.oficina": {"$regex": re.escape(ofi), "$options": "i"}},
                {"oficina": {"$regex": re.escape(ofi), "$options": "i"}},
            ]})

    if filtros.get("operacion"):
        op = filtros["operacion"]
        if op == "Venta":
            clauses.append({"$or": [
                {"tipo_operacion.venta": True},
                {"operacion": {"$regex": r"^Venta", "$options": "i"}},
            ]})
        elif op == "Arriendo":
            clauses.append({"$or": [
                {"tipo_operacion.arriendo": True},
                {"operacion": {"$regex": r"^Arriendo", "$options": "i"}},
            ]})

    if filtros.get("tipo"):
        tipo = filtros["tipo"]
        clauses.append({"$or": [
            {"tipo_operacion.tipo": {"$regex": re.escape(tipo), "$options": "i"}},
            {"metadata.tipo_propiedad": {"$regex": re.escape(tipo), "$options": "i"}},
            {"resumen.snapshot_listado.tipo": {"$regex": re.escape(tipo), "$options": "i"}},
            {"tipo": {"$regex": re.escape(tipo), "$options": "i"}},
        ]})

    if comunas:
        # Regex robusta para Maipu/Maipú sobre los campos anidados y planos.
        regex_list = [re.compile(get_accent_regex(c), re.IGNORECASE) for c in comunas]
        clauses.append({"$or": [
            {"ubicacion.comuna": {"$in": regex_list}},
            {"resumen.snapshot_listado.comuna": {"$in": regex_list}},
            {"comuna": {"$in": regex_list}},
        ]})

    if filtros.get("dormitorios"):
        d_val = filtros["dormitorios"]
        # Coincidencia EXACTA por defecto ("departamento 1 dormitorio" → solo de 1 dorm;
        # "casa 3 dormitorios" → solo de 3). Se respeta incluso en búsquedas relajadas.
        # Solo usa mínimo (>=) cuando el usuario pide "desde/al menos N dormitorios".
        if filtros.get("dormitorios_exacto"):
            clauses.append({"$or": [
                {"caracteristicas.dormitorios": d_val},
                {"dormitorios": {"$in": [str(d_val), d_val]}},
            ]})
        else:
            clauses.append({"$or": [
                {"caracteristicas.dormitorios": {"$gte": d_val}},
                {"dormitorios": {"$in": [str(i) for i in range(d_val, 15)] + [i for i in range(d_val, 15)]}},
            ]})

    if filtros.get("banos"):
        b_val = filtros["banos"]
        if filtros.get("banos_exacto"):
            clauses.append({"$or": [
                {"caracteristicas.banos": b_val},
                {"banos": {"$in": [str(b_val), b_val]}},
            ]})
        else:
            clauses.append({"$or": [
                {"caracteristicas.banos": {"$gte": b_val}},
                {"banos": {"$in": [str(i) for i in range(b_val, 15)] + [i for i in range(b_val, 15)]}},
            ]})

    if filtros.get("estacionamientos"):
        e_val = filtros["estacionamientos"]
        clauses.append({"$or": [
            {"caracteristicas.estacionamientos": {"$gte": e_val}},
            {"caracteristicas.estacionamientos_cubiertos": {"$gte": e_val}},
            {"caracteristicas.estacionamientos_descubiertos": {"$gte": e_val}},
            {"estacionamientos": {"$in": [str(i) for i in range(e_val, 15)] + [i for i in range(e_val, 15)]}},
        ]})

    if filtros.get("m2_utiles"):
        sup = filtros["m2_utiles"]
        paths = _superficie_paths(filtros.get("tipo"))
        clauses.append({"$or": [{path: {"$gte": sup}} for path in paths]})

    # Precios (rango no excluyente) - SOLO SI FUERON MENCIONADOS.
    # Respeta la operación detectada: Venta solo filtra precio_venta, Arriendo solo precio_arriendo.
    op_filtro = filtros.get("operacion")
    precio_uf = None
    if filtros.get("precio_uf_max") or filtros.get("precio_uf_min"):
        precio_uf = {}
        if filtros.get("precio_uf_max"):
            precio_uf["$lte"] = filtros["precio_uf_max"]
        if filtros.get("precio_uf_min"):
            precio_uf["$gte"] = filtros["precio_uf_min"]
        if op_filtro == "Venta":
            clauses.append({"$or": [
                {"tipo_operacion.precio_venta.precio_uf": precio_uf},
                {"precio_uf": precio_uf},
            ]})
        elif op_filtro == "Arriendo":
            clauses.append({"$or": [
                {"tipo_operacion.precio_arriendo.precio_uf": precio_uf},
                {"precio_uf": precio_uf},
            ]})
        else:
            clauses.append({"$or": [
                {"tipo_operacion.precio_venta.precio_uf": precio_uf},
                {"tipo_operacion.precio_arriendo.precio_uf": precio_uf},
                {"precio_uf": precio_uf},
            ]})

    if filtros.get("precio_clp_max") or filtros.get("precio_clp_min"):
        precio_clp = {}
        if filtros.get("precio_clp_max"):
            precio_clp["$lte"] = filtros["precio_clp_max"]
        if filtros.get("precio_clp_min"):
            precio_clp["$gte"] = filtros["precio_clp_min"]
        if op_filtro == "Venta":
            clauses.append({"$or": [
                {"tipo_operacion.precio_venta.precio_clp": precio_clp},
                {"precio_clp": precio_clp},
            ]})
        elif op_filtro == "Arriendo":
            clauses.append({"$or": [
                {"tipo_operacion.precio_arriendo.precio_clp": precio_clp},
                {"precio_clp": precio_clp},
            ]})
        else:
            clauses.append({"$or": [
                {"tipo_operacion.precio_venta.precio_clp": precio_clp},
                {"tipo_operacion.precio_arriendo.precio_clp": precio_clp},
                {"precio_clp": precio_clp},
            ]})

    # CRITICAL: Solamente propiedades disponibles
    clauses.append({"$or": [
        {"disponible_prop360": True},
        {"disponible": True},
    ]})

    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def buscar_semanticamente(query_text: str, limit: int = 3, 
                          oficina_filtro: str = "PROCASA SUCRE",
                          exclude_codes: list = None,
                          include_neighbors: bool = False,
                          criterios_estructurados: Dict = None) -> List[Dict]:
    """
    BÚSQUEDA HÍBRIDA:
    1) Extrae filtros duros del texto (tipo, operación, dormitorios, precio, comuna...)
    2) Combina con criterios estructurados del prospecto (si se entregan)
    3) Filtra MongoDB con esos campos estructurados (rápido y preciso)
    4) Rankea los resultados filtrados por similaridad semántica (piscina, sol, metro...)
    5) Fallback geográfico a comunas vecinas si hay pocos resultados
    """
    db = get_db()
    collection = db[Config.COLLECTION_NAME]

    # --- Paso 1: Extraer filtros estructurados ---
    filtros, target_commune = extraer_filtros_estructurados(query_text)

    # Mezcla con criterios estructurados del prospecto (no sobreescribe lo detectado en texto)
    if criterios_estructurados:
        filtros, target_commune = _mezclar_criterios(filtros, target_commune, criterios_estructurados)

    # Support multiple communes from extraction
    extracted_communes = filtros.get("comunas", [])
    if target_commune and target_commune not in extracted_communes:
        extracted_communes.insert(0, target_commune)
    
    logger.info(f"[RAG-HYBRID] Filtros extraidos: {filtros} | Comunas: {extracted_communes}")

    # --- Paso 2: Generar vector del query (Solo si es necesario) ---
    use_semantic = needs_semantic(query_text) or bool(criterios_estructurados)
    query_vector = None
    if use_semantic:
        logger.info("[RAG-HYBRID] Generando embedding para búsqueda semántica...")
        query_vector = generate_embedding(query_text)
        if query_vector is None:
            logger.warning("[RAG-HYBRID] Modelo no disponible; bajando a ranking por filtros")
            use_semantic = False

    # --- Paso 3: Helper de búsqueda vectorial con filtros ---
    excluded = set(exclude_codes or [])

    def ejecutar_busqueda(comunas: List[str] = None, relajar_filtros: bool = False, global_scope: bool = False):
        nonlocal use_semantic
        # El filtro de oficina se mantiene SIEMPRE que se solicitó (scope local).
        # global_scope solo amplía la zona geográfica, no la oficina.
        target_office = _normalizar_oficina(oficina_filtro) if oficina_filtro else None
        if relajar_filtros:
            # RELAXED: Remove M2 but KEEP PRICE and BEDROOMS (User requested strict bedrooms)
            filtros_relajados = {k: v for k, v in filtros.items() 
                               if k in ["operacion", "tipo", "precio_uf_max", "precio_uf_min", "precio_clp_max", "precio_clp_min", "dormitorios", "banos", "dormitorios_exacto", "banos_exacto"]}
            mongo_query = _construir_filtros_mongo(filtros_relajados, comunas, target_office, relajar_filtros=True)
        else:
            mongo_query = _construir_filtros_mongo(filtros, comunas, target_office, relajar_filtros=False)

        projection = {
            "vector_descripcion": 1, "codigo": 1, "oficina_nombre": 1,
            "comuna": 1, "operacion": 1, "tipo": 1, "precio_uf": 1, "precio_clp": 1,
            "dormitorios": 1, "banos": 1, "m2_utiles": 1, "descripcion_clean": 1,
            "nombre_calle": 1,
            "resumen": 1, "tipo_operacion": 1, "ubicacion": 1, "caracteristicas": 1,
            "observaciones": 1, "metadata": 1, "datos_propietario": 1, "publicaciones": 1,
        }

        candidatos = list(collection.find(mongo_query, projection).limit(2000))
        if use_semantic:
            vectors = [c.get("vector_descripcion") for c in candidatos if c.get("vector_descripcion")]
            if not candidatos:
                logger.warning(f"[RAG-HYBRID] Cero candidatos para el filtro (oficina={target_office!r}, comunas={comunas!r}, relajar={relajar_filtros})")
            elif not vectors:
                logger.warning("[RAG-HYBRID] Candidatos sin vector_descripcion, bajando a ranking por filtros")
            if not vectors:
                use_semantic = False
                
        # Ranking
        scored = []
        if use_semantic:
            t0 = time.time()
            sims = cosine_similarity([query_vector], vectors)[0]
            logger.info(f"[RAG-HYBRID] Similaridad calculada en {time.time()-t0:.3f}s")
            
            scored_candidates = candidatos if len(vectors) == len(candidatos) else [c for c in candidatos if c.get("vector_descripcion")]
            for idx, cand in enumerate(scored_candidates):
                if cand.get("codigo") in excluded: continue
                score = float(sims[idx])
                # Boost comuna exacta (+15% score)
                if target_commune and cand.get("comuna", "").lower() == target_commune.lower():
                    score += 0.15
                elif target_commune and (cand.get("ubicacion") or {}).get("comuna", "").lower() == target_commune.lower():
                    score += 0.15
                scored.append((score, cand))
        else:
            # Ranking básico por filtros (score base 1.0)
            for cand in candidatos:
                if cand.get("codigo") in excluded: continue
                score = 1.0
                comuna_cand = (cand.get("ubicacion") or {}).get("comuna") or cand.get("comuna") or ""
                if target_commune and comuna_cand.lower() == target_commune.lower():
                    score += 0.20 
                scored.append((score, cand))

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
