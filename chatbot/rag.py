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

# Palabras de dirección MÍNIMO que deben estar ADYACENTES al número de
# dormitorios/baños para degradar exacto → mínimo (evita "desde la casa").
_RE_MIN_PREF = (r'(?:desde|al\s+menos|a\s+lo\s+menos|por\s+lo\s+menos|mínimo|minimo|'
                r'como\s+mínimo|como\s+minimo|más\s+de|mas\s+de)')
_RE_DORMS_MIN = re.compile(
    rf'(?:{_RE_MIN_PREF})\s*(\d{{1,2}})\s*(?:dormitorio|dorm|pieza|habitaci[oó]n)'
    rf'(?:\s*(?:o\s+más|o\s+mas|\+))?', re.IGNORECASE)
_RE_DORMS_PALABRA_MIN = re.compile(
    rf'(?:{_RE_MIN_PREF})\s*'
    rf'\b(un\s+solo|una\s+sola|un|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\b'
    rf'\s+(?:solo|sola)?\s*(?:dormitorio|dorm|pieza|habitaci[oó]n)', re.IGNORECASE)
_RE_BANOS_MIN = re.compile(
    rf'(?:{_RE_MIN_PREF})\s*(\d{{1,2}})\s*(?:ba[ñn]o)'
    rf'(?:\s*(?:o\s+más|o\s+mas|\+))?', re.IGNORECASE)
_RE_BANOS_PALABRA_MIN = re.compile(
    rf'(?:{_RE_MIN_PREF})\s*'
    rf'\b(un\s+solo|una\s+sola|un|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\b'
    rf'\s+(?:solo|sola)?\s*(?:ba[ñn]o)', re.IGNORECASE)
# ------------------------------------------------------------------
# OPERACIÓN (Venta/Arriendo) — word boundaries, cubre arrendar/alquilar.
# ------------------------------------------------------------------
_RE_OP_ARR = re.compile(
    r'\b(?:arrendar|arriendo|arrienda|arriendan|arriendas|arrienden|arrendamiento|arrendador|'
    r'alquiler|alquilar|alquila|alquilan|renta|rentar|rento|rentamos|rentan)\b', re.IGNORECASE)
_RE_OP_VENTA = re.compile(
    r'\b(?:venta|ventas|vender|vendo|vendes|venden|vende|'
    r'compra|comprar|compro|compramos|compran|compras|'
    r'adquirir|adquisición|adquisicion)\b', re.IGNORECASE)

# ------------------------------------------------------------------
# PRECIOS — UF y CLP con dirección de precio ADYACENTE (no substring).
# La dirección (hasta/desde/máximo/mínimo/...) forma parte del patrón
# de precio. Una palabra "desde"/"min" en otra parte del texto NO influye
# (ej: "trabajo desde la casa ... hasta 7.000 UF" → precio_uf_max=7000).
# ------------------------------------------------------------------
_PRECIO_AMT = r'(?:\d{1,3}(?:[.,]\d{3})+|\d+)'
_PRECIO_MIL = r'mil'
# OJO: "millones?" debe ir ANTES de "mil" porque "millones" empieza con "mil".
_PRECIO_SCALE = r'millones?|millón|mm|mil|pesos|palos?'
_PRECIO_DIR = (r'(?:no\s+más\s+de|no\s+mas\s+de|menos\s+de|más\s+de|mas\s+de|'
               r'superior\s+a|al\s+menos|desde|mínimo|minimo|mínima|minima|sobre|'
               r'hasta|máximo|maximo|máxima|maxima|tope)')
# Dirección MIN/MAX en forma normalizada (sin acentos, minúsculas).
_PRECIO_DIR_MIN = {"desde", "minimo", "minima", "sobre", "mas de", "superior a", "al menos"}
_PRECIO_DIR_MAX = {"hasta", "maximo", "maxima", "menos de", "no mas de", "tope"}

_RE_PRECIO_UF = re.compile(
    rf'(?P<dir>\b{_PRECIO_DIR}\b)?\s*'
    rf'(?:'
    rf'    (?P<a1>{_PRECIO_AMT})(?:\s*(?P<am1>{_PRECIO_MIL}))?'
    rf'    (?:[ ](?:a|y(?:[ ]hasta)?|-)[ ](?P<a2>{_PRECIO_AMT})(?:\s*(?P<am2>{_PRECIO_MIL}))?)?'
    rf'    [ ]uf\b'
    rf'  |'
    rf'    uf[ ](?P<b1>{_PRECIO_AMT})(?:\s*(?P<bm1>{_PRECIO_MIL}))?'
    rf')',
    re.IGNORECASE | re.VERBOSE)

_RE_PRECIO_CLP = re.compile(
    rf'(?P<dir>\b{_PRECIO_DIR}\b)?\s*'
    rf'(?:'
    rf'    \$\s*(?P<d1>{_PRECIO_AMT})(?:\s*(?P<ds1>{_PRECIO_SCALE}))?'
    rf'  |'
    rf'    (?P<e1>{_PRECIO_AMT})\s+(?P<es1>{_PRECIO_SCALE})'
    rf')'
    rf'(?:[ ](?:a|y(?:[ ]hasta)?|-)[ ]'
    rf'    (?:'
    rf'        \$\s*(?P<d2>{_PRECIO_AMT})(?:\s*(?P<ds2>{_PRECIO_SCALE}))?'
    rf'      |'
    rf'        (?P<e2>{_PRECIO_AMT})\s+(?P<es2>{_PRECIO_SCALE})'
    rf'    )'
    rf')?',
    re.IGNORECASE | re.VERBOSE)


def _dir_precio_tipo(dir_word: Optional[str]) -> Optional[str]:
    """Devuelve 'min' o 'max' según la palabra de dirección del precio."""
    if not dir_word:
        return None
    d = remove_accents(dir_word.strip().lower())
    if d in _PRECIO_DIR_MIN:
        return "min"
    if d in _PRECIO_DIR_MAX:
        return "max"
    return None


def _parse_monto(text: str) -> float:
    """Convierte '6.000' o '6,000' o '$120.000' o '180.000.000' a número."""
    s = (text or "").strip().lstrip("$").replace(".", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _apply_scale(val: float, scale: Optional[str]) -> float:
    if not scale:
        return val
    sc = remove_accents(scale.strip().lower())
    if sc == "mil":
        return val * 1_000
    if sc.startswith("millon") or sc == "mm":
        return val * 1_000_000
    if sc in ("palo", "palos"):
        return val * 1_000_000
    return val


# ------------------------------------------------------------------
# ORIENTACIÓN — dato estructurado `caracteristicas.orientacion`
# HARD cuando el usuario usa contexto explícito ("orientación X",
# "orientado al X", "exposición X"). Un punto cardinal suelto ("norte")
# NO se interpreta como orientación (evita "Santiago Norte").
# ------------------------------------------------------------------
_ORIENTACION_PUNTOS = (
    r'norte|sur|oriente|poniente|este|oeste'
    r'|nor[- ]?oriente|nororiente|noreste|nor[- ]?poniente|norponiente|noroeste|noroccidente'
    r'|sur[- ]?oriente|suroriente|sur[- ]?poniente|surponiente|suroeste|suroccidente'
)
_RE_ORIENTACION = re.compile(
    rf'\b(?:orientaci[oó]n|orientad[oa]s?|exposici[oó]n|expuesto\s+(?:al|a|hacia|hac[ií]a))\b'
    rf'(?:\s+(?:hacia|hac[ií]a|al|a|de|por|en|el|la))?\s*'
    rf'(?P<punto>\b(?:{_ORIENTACION_PUNTOS})\b)',
    re.IGNORECASE)

# Componentes cardinales base de cada orientación canónica.
_ORIENTACION_CANON = {
    "Norte": {"Norte"}, "Sur": {"Sur"}, "Oriente": {"Oriente"}, "Poniente": {"Poniente"},
    "Nor-Oriente": {"Norte", "Oriente"}, "Nor-Poniente": {"Norte", "Poniente"},
    "Sur-Oriente": {"Sur", "Oriente"}, "Sur-Poniente": {"Sur", "Poniente"},
}


def normalizar_orientacion(valor) -> Optional[str]:
    """Normaliza variantes reales de la BD/consulta a orientación canónica.
    Documenta el dato anómalo 'NorPoniente-Sur' (6 docs) → Nor-Poniente."""
    if not valor:
        return None
    v = remove_accents(str(valor)).strip().lower()
    v = re.sub(r'[^a-z]', ' ', v)
    v = re.sub(r'\s+', '-', v).strip('-')
    tab = {
        'norte': 'Norte', 'sur': 'Sur', 'oriente': 'Oriente', 'poniente': 'Poniente',
        'este': 'Oriente', 'oeste': 'Poniente',
        'nor-oriente': 'Nor-Oriente', 'nororiente': 'Nor-Oriente', 'noreste': 'Nor-Oriente',
        'nor-este': 'Nor-Oriente',
        'nor-poniente': 'Nor-Poniente', 'norponiente': 'Nor-Poniente', 'noroeste': 'Nor-Poniente',
        'nor-oeste': 'Nor-Poniente',
        'norponiente-sur': 'Nor-Poniente', 'nor-poniente-sur': 'Nor-Poniente',
        'sur-oriente': 'Sur-Oriente', 'suroriente': 'Sur-Oriente', 'sureste': 'Sur-Oriente',
        'sur-este': 'Sur-Oriente',
        'sur-poniente': 'Sur-Poniente', 'surponiente': 'Sur-Poniente', 'suroeste': 'Sur-Poniente',
        'sur-oeste': 'Sur-Poniente',
    }
    return tab.get(v)


def _componentes_orientacion(canon: str) -> set:
    return set(_ORIENTACION_CANON.get(canon, ()))


def orientacion_compatible(requerida: str, candidata: str) -> bool:
    """True si la orientación canónica candidata es compatible con la
    requerida. Cardinal simple (Norte) acepta los compuestos que lo incluyen
    (Norte, Nor-Oriente, Nor-Poniente); compuesto explícito (Nor-Oriente)
    SOLO acepta ese compuesto (no se amplía arbitrariamente)."""
    return candidata in _orientaciones_compatibles(requerida)


def _orientaciones_compatibles(requerida: str) -> set:
    """Conjunto de orientaciones canónicas compatibles con la requerida.
    Fuente de verdad ÚNICA compartida por el query Mongo y el post-filtro,
    para que nunca exista diferencia entre pre-filtro y post-filtro."""
    can = normalizar_orientacion(requerida)
    if not can:
        return set()
    comp = _componentes_orientacion(can)
    if len(comp) == 1:
        # Cardinal simple: acepta el punto cardinal y los compuestos que lo incluyen.
        return {c for c, comps in _ORIENTACION_CANON.items() if comps & comp}
    # Compuesto explícito: solo ese compuesto (evita búsqueda arbitrariamente amplia).
    return {can}


# Patrones raw FULL-MATCH (no substring): cada alternativa matchea el valor
# COMPLETO del campo, igual que normalizar_orientacion resuelve la clave exacta.
# Sin full-match, '\bsur\b' matchearía el "Sur" final del dato anómalo
# "NorPoniente-Sur" (que NORMALIZA a Nor-Poniente, sin componente Sur).
# Deben cubrir TODAS las variantes que normalizar_orientacion acepta, incl.
# el dato anómalo "NorPoniente-Sur" (6 docs) que normaliza a Nor-Poniente.
_ORIENTACION_RAW = {
    "Norte": r'^\s*norte\s*$',
    "Sur": r'^\s*sur\s*$',
    "Oriente": r'^\s*(?:oriente|este)\s*$',
    "Poniente": r'^\s*(?:poniente|oeste)\s*$',
    "Nor-Oriente": r'^\s*(?:nor[\s-]?oriente|nororiente|noreste|nor[\s-]?este)\s*$',
    "Nor-Poniente": (r'^\s*(?:nor[\s-]?poniente|norponiente|noroeste|nor[\s-]?oeste'
                     r'|norponiente[\s-]?sur|nor[\s-]?poniente[\s-]?sur)\s*$'),
    "Sur-Oriente": r'^\s*(?:sur[\s-]?oriente|suroriente|sureste|sur[\s-]?este)\s*$',
    "Sur-Poniente": r'^\s*(?:sur[\s-]?poniente|surponiente|suroeste|sur[\s-]?oeste)\s*$',
}


def _regex_orientacion_compatible(requerida: str):
    """Regex Mongo con la MISMA semántica de componentes que el post-filtro
    (_orientaciones_compatibles): matchea cualquier valor raw cuya orientación
    canónica sea compatible con la requerida (Norte → Norte, Nor-Oriente,
    Nor-Poniente; Nor-Oriente → solo Nor-Oriente)."""
    can = normalizar_orientacion(requerida)
    if not can:
        return None
    compatibles = _orientaciones_compatibles(can)
    if not compatibles:
        return None
    patrones = "|".join(_ORIENTACION_RAW[c] for c in compatibles if c in _ORIENTACION_RAW)
    if not patrones:
        return None
    return re.compile(r'(?:' + patrones + r')', re.IGNORECASE)


def _extraer_orientacion(texto: str) -> Optional[str]:
    for m in _RE_ORIENTACION.finditer(texto):
        canon = normalizar_orientacion(m.group("punto"))
        if canon:
            return canon
    return None


# ------------------------------------------------------------------
# GASTOS COMUNES — `tipo_operacion.gastos_comunes`
# HARD cuando hay restricción numérica. Los montos de GC se identifican
# por contexto ("gastos comunes/GC") y se MASCAN para que el parser de
# precio de la propiedad NO los vuelva a capturar como precio_clp.
# ------------------------------------------------------------------
_RE_GC_KW = re.compile(r'\bgastos?\s+comunes?\b|\bgc\b', re.IGNORECASE)
_RE_GC_MONTO = re.compile(
    r'(?P<dir>hasta|m[aá]ximo|m[aá]xima|tope|menos\s+de|'
    r'no\s+(?:m[aá]s\s+de|super(?:e|en|ar|iores?\s+a)|sobrepase?|sobrepasar|exceda)|'
    r'menor(?:es)?\s+a|inferior(?:es)?\s+a|desde|m[ií]nimo|m[ií]nima|al\s+menos|m[aá]s\s+de|sobre)?'
    r'\s*(?P<amt>\$\s*\d{1,3}(?:[.,]\d{3})+|\$\s*\d+|\d{1,3}(?:[.,]\d{3})+|\d+)'
    r'(?:\s*(?P<scale>mil|pesos|palos?|millones?|millón))?'
    r'(?!\s*(?:uf|u\s*\.\s*f))',
    re.IGNORECASE)
_RE_GC_ENTRE = re.compile(
    r'\bentre\s+(?P<a1>\$\s*\d{1,3}(?:[.,]\d{3})+|\$\s*\d+|\d{1,3}(?:[.,]\d{3})+|\d+)'
    r'\s*(?P<s1>mil|pesos|palos?|millones?|millón)?\s*(?:y|-)\s*'
    r'(?P<a2>\$\s*\d{1,3}(?:[.,]\d{3})+|\$\s*\d+|\d{1,3}(?:[.,]\d{3})+|\d+)'
    r'\s*(?P<s2>mil|pesos|palos?|millones?|millón)?', re.IGNORECASE)


def _extraer_gastos_comunes(texto: str) -> tuple:
    """Retorna (filtros_gc, spans_absolutos_a_mascarar).
    Solo cuenta como GC un monto con $, con escala, o >= 10.000 (evita que
    '6000 UF' de precio sea tomado como GC)."""
    filtros: Dict = {}
    spans = []
    for m in _RE_GC_KW.finditer(texto):
        seg_start = m.end()
        msep = re.search(r'[!?]|\n|\.(?=\s|$)', texto[seg_start:])
        seg_end = seg_start + (msep.start() if msep else len(texto) - seg_start)
        seg = texto[seg_start:seg_end]

        mr = _RE_GC_ENTRE.search(seg)
        if mr:
            v1 = _apply_scale(_parse_monto(mr.group("a1")), mr.group("s1"))
            v2 = _apply_scale(_parse_monto(mr.group("a2")), mr.group("s2"))
            filtros["gastos_comunes_min"] = int(min(v1, v2))
            filtros["gastos_comunes_max"] = int(max(v1, v2))
            spans.append((seg_start + mr.start(), seg_start + mr.end()))
            continue

        for mm in _RE_GC_MONTO.finditer(seg):
            amt_text = mm.group("amt")
            tiene_dolar = amt_text.strip().startswith("$")
            scale = mm.group("scale")
            val = _apply_scale(_parse_monto(amt_text), scale)
            if val <= 0:
                continue
            if not tiene_dolar and not scale and val < 10000:
                continue
            d = remove_accents((mm.group("dir") or "").strip().lower())
            if d in ("desde", "minimo", "minima", "al menos", "mas de", "sobre"):
                filtros["gastos_comunes_min"] = int(max(filtros.get("gastos_comunes_min", 0), val))
            else:
                filtros["gastos_comunes_max"] = int(max(filtros.get("gastos_comunes_max", 0), val))
            spans.append((seg_start + mm.start(), seg_start + mm.end()))
    return filtros, spans


def _enmascarar(texto: str, spans: List) -> str:
    if not spans:
        return texto
    lista = list(texto)
    for s, e in spans:
        for i in range(max(0, s), min(e, len(lista))):
            if lista[i] != '\n':
                lista[i] = ' '
    return "".join(lista)


# ------------------------------------------------------------------
# BODEGAS — atributo `caracteristicas.bodegas` (NO tipo de propiedad).
# Filosofía exacto/mínimo igual que dormitorios. "sin bodega" → 0 exacto.
# Guard de dato anómalo bodegas=319 (código 6199): se excluye como cantidad
# razonable (<= 30) sin modificar Mongo.
# ------------------------------------------------------------------
_RE_BODEGAS = re.compile(r'(\d{1,3})\s*(?:bodega|bodegas)', re.IGNORECASE)
_RE_NUM_PALABRA = r'(?:un\s+solo|una\s+sola|un|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)'
_RE_BODEGAS_PALABRA = re.compile(
    r'\b(' + _RE_NUM_PALABRA + r')\s+'
    r'(?:solo|sola)?\s*(?:bodega|bodegas)', re.IGNORECASE)
_RE_BODEGAS_MIN = re.compile(
    rf'(?:{_RE_MIN_PREF})\s*(\d{{1,2}})\s*(?:bodega|bodegas)(?:\s*(?:o\s+m[aá]s|\+))?', re.IGNORECASE)
_RE_BODEGAS_PALABRA_MIN = re.compile(
    rf'(?:{_RE_MIN_PREF})\s*\b({_RE_NUM_PALABRA})\b\s*'
    rf'(?:solo|sola)?\s*(?:bodega|bodegas)', re.IGNORECASE)
_RE_BODEGAS_NEG = re.compile(r'\b(?:sin|no\s+(?:tiene|posee|cuenta\s+con|incluye))\s+bodega', re.IGNORECASE)


def _extraer_bodegas(texto: str) -> Optional[Dict]:
    if _RE_BODEGAS_NEG.search(texto):
        return {"bodegas": 0, "bodegas_exacto": True}
    m = _RE_BODEGAS.search(texto)
    if not m:
        m = _RE_BODEGAS_PALABRA.search(texto)
        if m:
            val = _NUMEROS_PALABRA.get(m.group(1).lower().split()[0], 1)
            es_min = bool(_RE_BODEGAS_PALABRA_MIN.search(texto))
            return {"bodegas": val, "bodegas_exacto": not es_min}
        return None
    val = int(m.group(1))
    es_min = bool(_RE_BODEGAS_MIN.search(texto))
    return {"bodegas": val, "bodegas_exacto": not es_min}


# ------------------------------------------------------------------
# ESTACIONAMIENTOS — `caracteristicas.estacionamientos` (+ cubiertos/
# descubiertos). Ahora también números en palabras y exacto/mínimo.
# ------------------------------------------------------------------
_RE_ESTAC = re.compile(r'(\d{1,2})\s*(?:estacionamiento|estacionamientos|parking|cochera|estac)', re.IGNORECASE)
_RE_ESTAC_PALABRA = re.compile(
    r'\b(' + _RE_NUM_PALABRA + r')\s+'
    r'(?:solo|sola)?\s*(?:estacionamiento|estacionamientos|parking|cochera|estac)', re.IGNORECASE)
_RE_ESTAC_MIN = re.compile(
    rf'(?:{_RE_MIN_PREF})\s*(\d{{1,2}})\s*(?:estacionamiento|estacionamientos|parking|cochera|estac)(?:\s*(?:o\s+m[aá]s|\+))?', re.IGNORECASE)
_RE_ESTAC_PALABRA_MIN = re.compile(
    rf'(?:{_RE_MIN_PREF})\s*\b({_RE_NUM_PALABRA})\b\s*'
    rf'(?:solo|sola)?\s*(?:estacionamiento|estacionamientos|parking|cochera|estac)', re.IGNORECASE)
_RE_ESTAC_NEG = re.compile(r'\b(?:sin|no\s+(?:tiene|posee|cuenta\s+con|incluye))\s+(?:estacionamiento|parking|cochera)', re.IGNORECASE)


def _extraer_estacionamientos(texto: str) -> Optional[Dict]:
    if _RE_ESTAC_NEG.search(texto):
        return {"estacionamientos": 0, "estacionamientos_exacto": True}
    m = _RE_ESTAC.search(texto)
    if not m:
        m = _RE_ESTAC_PALABRA.search(texto)
        if m:
            val = _NUMEROS_PALABRA.get(m.group(1).lower().split()[0], 1)
            es_min = bool(_RE_ESTAC_PALABRA_MIN.search(texto))
            return {"estacionamientos": val, "estacionamientos_exacto": not es_min}
        return None
    val = int(m.group(1))
    es_min = bool(_RE_ESTAC_MIN.search(texto))
    return {"estacionamientos": val, "estacionamientos_exacto": not es_min}


# ------------------------------------------------------------------
# PISO — `caracteristicas.piso`. Soporta exacto, "desde el piso N" (min)
# y "hasta el piso N" (max). NO conceptos ambiguos (piso alto/bajo).
# ------------------------------------------------------------------
_RE_PISO = re.compile(
    r'\b(?:en\s+el\s+piso\s+(\d{1,3})|al\s+piso\s+(\d{1,3})|el\s+piso\s+(\d{1,3})|\bpiso\s+(\d{1,3}))\b', re.IGNORECASE)
_RE_PISO_MIN = re.compile(
    r'\b(?:desde\s+el\s+piso\s+(\d{1,3})|desde\s+piso\s+(\d{1,3})|'
    r'piso\s+desde\s+(?:el\s+)?(\d{1,3})|'
    r'piso\s+(\d{1,3})\s+(?:o\s+(?:m[aá]s|superior)|en\s+adelante|hacia\s+arriba)|'
    r'piso\s+(\d{1,3})\s+\+)', re.IGNORECASE)
_RE_PISO_MAX = re.compile(
    r'\b(?:hasta\s+el\s+piso\s+(\d{1,3})|hasta\s+piso\s+(\d{1,3})|'
    r'piso\s+hasta\s+(?:el\s+)?(\d{1,3})|'
    r'piso\s+(\d{1,3})\s+(?:o\s+inferior|hacia\s+abajo)|'
    r'm[aá]ximo\s+piso\s+(\d{1,3}))', re.IGNORECASE)


def _extraer_piso(texto: str) -> Optional[Dict]:
    m = _RE_PISO_MIN.search(texto)
    if m:
        val = next(int(g) for g in m.groups() if g)
        return {"piso": val, "piso_dir": "min"}
    m = _RE_PISO_MAX.search(texto)
    if m:
        val = next(int(g) for g in m.groups() if g)
        return {"piso": val, "piso_dir": "max"}
    m = _RE_PISO.search(texto)
    if m:
        val = next(int(g) for g in m.groups() if g)
        return {"piso": val, "piso_dir": "exacto"}
    return None


# ------------------------------------------------------------------
# OPERACIÓN EN CONTEXTO DE INVERSIÓN — "fácil de arrendar"/"para
# arrendar" NO deben activar Arriendo. Solo cuenta arriendo/compra
# transaccional ("quiero arrendar", "en arriendo", "quiero comprar").
# ------------------------------------------------------------------
_RE_VERB_QUIERO = re.compile(
    r'\b(?:quier[oa]s?|queremos|busco|buscamos|busca|necesito|necesitamos|necesita|'
    r'me\s+interesa|estoy\s+(?:buscando|viendo)|estamos\s+buscando)\b', re.IGNORECASE)
_RE_INVERSION = re.compile(
    r'\b(?:inversi[oó]n|inversionista|invertir|rentabilidad|rentable|rentabilizar|'
    r'para\s+invertir|ideal\s+para\s+renta|ideal\s+para\s+arrendar|buena\s+renta|'
    r'demanda\s+de\s+arriendo|f[aá]cil\s+de\s+arrendar|para\s+arrendar(?:l[oa]s?)?|'
    r'arrendarl[oa]s?|rentarl[oa]s?)\b', re.IGNORECASE)
_RE_ARR_TRANSACCIONAL = re.compile(
    r'\ben\s+arriendo\b|\ben\s+arrendamiento\b|'
    r'\b(?:arriendo|arrienda|arriendan|arrendar|alquilar)\s+(?:un|una|el|la|este|ese|'
    r'depto|departamento|casa|propiedad|inmueble|oficina|local)\b',
    re.IGNORECASE)


def _detectar_operacion(texto: str) -> Optional[str]:
    q = texto.lower()
    hay_arr = bool(_RE_OP_ARR.search(q))
    hay_venta = bool(_RE_OP_VENTA.search(q))
    hay_inversion = bool(_RE_INVERSION.search(q))
    hay_intencion = bool(_RE_VERB_QUIERO.search(q))

    arr_verbal = bool(_RE_ARR_TRANSACCIONAL.search(q)) or (
        hay_intencion and re.search(r'\b(?:arrendar|arriendo|alquilar|rentar)\b', q))
    arr_descriptivo = bool(re.search(
        r'\b(?:f[aá]cil\s+de\s+arrendar|para\s+arrendar(?:l[oa]s?)?|arrendarl[oa]s?|'
        r'ideal\s+para\s+(?:arrendar|renta)|buena\s+renta|demanda\s+de\s+arriendo|'
        r'a\s+renta|rentabilidad|rentable|al\s+arriendo)\b', q))
    signal_arr = arr_verbal and not arr_descriptivo

    venta_verbal = hay_venta and (hay_intencion or re.search(r'\ben\s+venta\b', q))

    if signal_arr and not venta_verbal:
        return "Arriendo"
    if venta_verbal and not signal_arr:
        return "Venta"
    if signal_arr and venta_verbal:
        p_arr = _RE_OP_ARR.search(q)
        p_venta = _RE_OP_VENTA.search(q)
        return "Arriendo" if p_arr.start() < p_venta.start() else "Venta"
    if hay_inversion:
        if hay_venta or hay_intencion:
            return "Venta"
        return None
    if hay_arr and not hay_venta:
        return "Arriendo"
    if hay_venta and not hay_arr:
        return "Venta"
    if hay_arr and hay_venta:
        p_arr = _RE_OP_ARR.search(q)
        p_venta = _RE_OP_VENTA.search(q)
        return "Arriendo" if p_arr.start() < p_venta.start() else "Venta"
    return None


# ------------------------------------------------------------------
# COMUNAS SIMILARES / VECINOS DESDE TEXTO — activa fallback geográfico.
# ------------------------------------------------------------------
_RE_VECINOS = re.compile(
    r'\b(?:comunas?\s+(?:similares|vecinas?|cercanas|aleda[ñn]as)|'
    r'sectores?\s+cercanos|comunas?\s+aleda[ñn]as|alrededores?|'
    r'puede\s+ser\s+en\s+comunas?\s+similares|'
    r'si\s+no\s+hay[^.;!?]{0,40}(?:similares|vecinas?|cercanas|aleda[ñn]as|alrededor)|'
    r'o\s+(?:en\s+)?(?:comunas?|sectores?)\s+(?:similares|vecinas?|cercanas))\b',
    re.IGNORECASE)


def _detectar_vecinos(texto: str) -> bool:
    return bool(_RE_VECINOS.search(texto))


# ------------------------------------------------------------------
# PREFERENCIAS NEGATIVAS (SOFT) — capa post-ranking determinística.
# Penaliza (no elimina) propiedades cuyo texto contiene el término
# negado, salvo que el término aparezca en contexto positivo
# ("alejado del ruido", "sin piscina", "no tiene avenida", ...).
# ------------------------------------------------------------------
_NEG_LEXICON = {
    "ruido", "ruidosa", "ruidoso", "ruidosos", "avenida", "autopista", "carretera",
    "piscina", "gimnasio", "quincho", "bodega", "estacionamiento", "jardin", "terraza",
    "interior", "metro", "amoblado", "mascotas", "mascota", "norte", "sur",
    "oriente", "poniente",
}
_NEG_LEXICON_FRASE = {
    "primera linea", "primer piso", "piso bajo", "avenida principal", "avenida ruidosa",
    "segundo piso",
}
_NEG_NO_VERBOS = {"ser", "es", "soy", "estoy", "estamos", "esta", "hay", "quiero",
                  "puedo", "tengo", "tenemos", "necesito", "busco", "vivo", "se", "sabe"}
_RE_NEG_PATRONES = [
    re.compile(
        r'\b(?:no\s+(?:quier[oa]|queremos|necesito|necesitamos|busco|buscamos|deseo|deseamos|uso|usamos)|'
        r'evitar|evito|evitamos|prefiero\s+sin|mejor\s+sin|sin|que\s+no\s+tenga|'
        r'que\s+no\s+sea|nada\s+de)\s*'
        r'(?P<obj>(?:[a-záéíóúñü]{3,}\s*){1,3})', re.IGNORECASE),
    re.compile(r'\bno\s+(?P<obj>[a-záéíóúñü]{4,}(?:\s+[a-záéíóúñü]{4,}){0,2})', re.IGNORECASE),
]
_RE_MITIGACION = re.compile(
    r'(?:alejad|lejos\s+de|libre\s+de|\bsin\b|no\s+(?:tiene|presenta|posee|cuenta\s+con|incluye)|'
    r'aisl|protegid|fuera\s+de|sin\s+exposici)', re.IGNORECASE)


def _extraer_negaciones(query_text: str) -> List[str]:
    q = remove_accents(query_text.lower())
    terminos = []
    for pat in _RE_NEG_PATRONES:
        for m in pat.finditer(q):
            obj = remove_accents(m.group("obj").strip())
            palabras = re.split(r'\s+', obj)
            # Frase completa primero (ej: "primer piso", "avenida ruidosa").
            agregado = False
            for idx in range(len(palabras)):
                frase = " ".join(palabras[idx:])
                if frase in _NEG_LEXICON_FRASE:
                    terminos.append(frase)
                    agregado = True
                    break
            if agregado:
                continue
            for p in palabras:
                if p in _NEG_LEXICON and p not in terminos and p not in _NEG_NO_VERBOS:
                    terminos.append(p)
    return terminos


def _texto_tiene_termino_negativo(desc: str, termino: str) -> bool:
    d = remove_accents((desc or "").lower())
    t = remove_accents(termino.lower())
    checks = [t] + [p for p in t.split() if p in _NEG_LEXICON and p != t]
    for c in checks:
        for m in re.finditer(re.escape(c), d):
            ctx = d[max(0, m.start() - 40):m.start()]
            if not _RE_MITIGACION.search(ctx):
                return True
    return False


def _aplicar_penalizaciones_negativas(scored: List, query_text: str) -> List:
    terminos = _extraer_negaciones(query_text)
    if not terminos:
        return scored
    nuevos = []
    for score, doc in scored:
        desc = (doc.get("observaciones") or {}).get("descripcion") or doc.get("descripcion_clean") or ""
        pen = 0.0
        hits = []
        for t in terminos:
            if _texto_tiene_termino_negativo(desc, t):
                pen += 0.05
                hits.append(t)
        if hits:
            pen = min(pen, 0.15)
            logger.info(f"[RAG-HYBRID] Penalizacion negativa {doc.get('codigo')} {hits} -{pen:.2f}")
            nuevos.append((max(0.0, score - pen), doc))
        else:
            nuevos.append((score, doc))
    nuevos.sort(key=lambda x: x[0], reverse=True)
    return nuevos


# ------------------------------------------------------------------
# POST-FILTRO DE ATRIBUTOS VERIFICABLES — fuente de verdad para
# orientación, GC, bodegas, estacionamientos y piso. Los datos
# estructurados tienen prioridad absoluta sobre la similitud: una
# propiedad sin dato o con dato incompatible NO supera el filtro.
# ------------------------------------------------------------------
def _gc_doc_value(doc) -> object:
    to = doc.get("tipo_operacion") or {}
    v = to.get("gastos_comunes")
    if v is None:
        v = (doc.get("caracteristicas") or {}).get("gastos_comunes")
    if v is None:
        v = doc.get("gastos_comunes")
    return v


def _bodegas_doc_value(doc) -> object:
    car = doc.get("caracteristicas") or {}
    v = car.get("bodegas")
    if v is None:
        v = doc.get("bodegas")
    return v


def _estacionamientos_doc_value(doc) -> object:
    car = doc.get("caracteristicas") or {}
    v = car.get("estacionamientos")
    if v is None:
        cub = car.get("estacionamientos_cubiertos")
        descu = car.get("estacionamientos_descubiertos")
        if isinstance(cub, (int, float)) and isinstance(descu, (int, float)):
            v = cub + descu
        elif isinstance(cub, (int, float)):
            v = cub
        elif isinstance(descu, (int, float)):
            v = descu
    if v is None:
        v = doc.get("estacionamientos")
    return v


def _piso_doc_value(doc) -> object:
    car = doc.get("caracteristicas") or {}
    v = car.get("piso")
    if v is None:
        v = doc.get("piso")
    return v


def _post_filtrar_atributos(candidatos: List, filtros: Dict) -> List:
    if not candidatos:
        return candidatos
    orientacion = filtros.get("orientacion")
    gc_min = filtros.get("gastos_comunes_min")
    gc_max = filtros.get("gastos_comunes_max")
    b_val = filtros.get("bodegas")
    b_exacto = filtros.get("bodegas_exacto")
    e_val = filtros.get("estacionamientos")
    e_exacto = filtros.get("estacionamientos_exacto")
    p_val = filtros.get("piso")
    p_dir = filtros.get("piso_dir")
    tiene_gc = gc_min is not None or gc_max is not None

    res = []
    for d in candidatos:
        ok = True
        if ok and orientacion:
            raw = (d.get("caracteristicas") or {}).get("orientacion")
            canon = normalizar_orientacion(raw) if raw else None
            if not canon or not orientacion_compatible(orientacion, canon):
                ok = False
        if ok and tiene_gc:
            v = _gc_doc_value(d)
            if not isinstance(v, (int, float)) or v <= 0 or v < 1000:
                ok = False
            elif gc_min is not None and v < gc_min:
                ok = False
            elif gc_max is not None and v > gc_max:
                ok = False
        if ok and b_val is not None:
            v = _bodegas_doc_value(d)
            if not isinstance(v, (int, float)) or v < 0 or v > 30:
                ok = False
            elif b_exacto and v != b_val:
                ok = False
            elif not b_exacto and v < b_val:
                ok = False
        if ok and e_val is not None:
            v = _estacionamientos_doc_value(d)
            if not isinstance(v, (int, float)) or v < 0:
                ok = False
            elif e_exacto and v != e_val:
                ok = False
            elif not e_exacto and v < e_val:
                ok = False
        if ok and p_val is not None:
            v = _piso_doc_value(d)
            if not isinstance(v, (int, float)):
                ok = False
            elif p_dir == "min" and v < p_val:
                ok = False
            elif p_dir == "max" and v > p_val:
                ok = False
            elif p_dir not in ("min", "max") and v != p_val:
                ok = False
        if ok:
            res.append(d)
    return res


def _extraer_precio(texto: str) -> Dict:
    """Extrae presupuesto UF o CLP (min/max) de un texto. La dirección se
    infiere SOLO de la palabra adyacente al monto (regex de proximidad)."""
    res: Dict = {}
    m = _RE_PRECIO_UF.search(texto)
    span_uf = m.span() if m else None
    if m:
        tipo_dir = _dir_precio_tipo(m.group("dir"))
        if m.group("a2"):
            v1 = _parse_monto(m.group("a1"))
            v2 = _parse_monto(m.group("a2"))
            if m.group("am1"):
                v1 = _apply_scale(v1, "mil")
            if m.group("am2"):
                v2 = _apply_scale(v2, "mil")
            res["precio_uf_min"] = min(v1, v2)
            res["precio_uf_max"] = max(v1, v2)
        else:
            v1 = _parse_monto(m.group("a1") or m.group("b1"))
            sc = m.group("am1") or m.group("bm1")
            if sc:
                v1 = _apply_scale(v1, sc)
            if tipo_dir == "min":
                res["precio_uf_min"] = v1
            else:
                res["precio_uf_max"] = v1
    m = _RE_PRECIO_CLP.search(texto)
    span_clp = m.span() if m else None
    if m:
        tipo_dir = _dir_precio_tipo(m.group("dir"))
        if m.group("d1"):
            v1 = _parse_monto(m.group("d1"))
            v1 = _apply_scale(v1, m.group("ds1"))
        else:
            v1 = _parse_monto(m.group("e1"))
            v1 = _apply_scale(v1, m.group("es1"))
        v2 = None
        if m.group("d2"):
            v2 = _parse_monto(m.group("d2"))
            v2 = _apply_scale(v2, m.group("ds2"))
        elif m.group("e2"):
            v2 = _parse_monto(m.group("e2"))
            v2 = _apply_scale(v2, m.group("es2"))
        if v2 is not None:
            res["precio_clp_min"] = min(v1, v2)
            res["precio_clp_max"] = max(v1, v2)
        else:
            if tipo_dir == "min":
                res["precio_clp_min"] = v1
            else:
                res["precio_clp_max"] = v1
    # Si el match CLP se solapa con el match UF, es la MISMA expresión
    # (ej: "5 mil y 7 mil UF") y no debe marcarse como presupuesto dual.
    res["_overlap_clp_uf"] = bool(
        span_uf and span_clp and span_uf[0] <= span_clp[1] and span_uf[1] >= span_clp[0])
    return res


# ------------------------------------------------------------------
# TIPO DE PROPIEDAD — menciones incidentales ("desde la casa"/home-office)
# no clasifican como inmueble; conflictos se resuelven por intención.
# ------------------------------------------------------------------
_INCIDENTAL_CASA = [
    re.compile(r'\b(?:desde|en)\s+(?:(?:mi|tu|su|la|el|nuestra|nuestras|nuestro|nuestros|mis|tus|sus|las|los)\s+)?casa\b', re.IGNORECASE),
    re.compile(r'\btrabaj\w*\s+(?:desde\s+)?(?:(?:mi|la|nuestra|nuestras|nuestros)\s+)?casa\b', re.IGNORECASE),
    re.compile(r'\bteletrabaj\w*\s+(?:desde\s+)?(?:(?:mi|la|nuestra|nuestras|nuestros)\s+)?casa\b', re.IGNORECASE),
    re.compile(r'\b(?:quedarnos|quedarme|quedarse)\s+en\s+(?:la\s+)?casa\b', re.IGNORECASE),
]

# "terreno" es ambiguo: en "casa con buen terreno / y un terreno que permita
# ampliar" es DESCRIPTIVO del predio, no un listing tipo Sitio. Solo cuenta como
# tipo si un verbo de intención aparece justo antes (ej: "busco un terreno").
_INCIDENTAL_TERRENO = re.compile(
    r'\b(?:con|de|su|mi|el|la|un|una|este|ese|buen|buena|gran|grande|amplio|amplia|suficiente|y)\s+terreno\b'
    r'|\bterreno\s+(?:que|donde|para|permite|permita|disponible)\b',
    re.IGNORECASE)

_RE_INTENT_TIPO = re.compile(
    r'\b(?:estoy buscando|estamos buscando|busco|buscamos|busca|quiero|queremos|quiere|'
    r'necesito|necesitamos|necesita|me interesa|vendo|vender|arriendo|arrendar|alquilar|'
    r'para comprar|para vivir|para arrendar|en venta|en arriendo)\b', re.IGNORECASE)


def _detectar_tipo(texto: str) -> Optional[str]:
    """Detecta el tipo de inmueble explícito con word boundaries, ignorando
    menciones incidentales tipo 'trabajo desde la casa'. Si hay varios tipos
    candidatos, resuelve por proximidad a un verbo/patrón de intención."""
    q = texto.lower()
    menciones = []  # (pos, tipo)
    for keyword, tipo_val in MAP_TIPO.items():
        for m in re.finditer(rf'\b{re.escape(keyword)}\b', q):
            menciones.append((m.start(), tipo_val))
    if not menciones:
        return None

    spans_incidentales = []
    for pat in _INCIDENTAL_CASA:
        for m in pat.finditer(q):
            spans_incidentales.append((m.start(), m.end()))
    # "terreno" descriptivo: incidental salvo que haya intención justo antes.
    for m in _INCIDENTAL_TERRENO.finditer(q):
        pref = q[max(0, m.start() - 30):m.start()]
        if not _RE_INTENT_TIPO.search(pref):
            spans_incidentales.append((m.start(), m.end()))

    def es_incidental(pos):
        return any(s <= pos < e for s, e in spans_incidentales)

    intent = [m for m in menciones if not es_incidental(m[0])]
    if not intent:
        return None

    unicos = list(dict.fromkeys(t for _, t in intent))
    if len(unicos) == 1:
        return unicos[0]

    # Conflicto entre tipos: preferir el más cercano a un patrón de intención.
    verb_pos = [m.start() for m in _RE_INTENT_TIPO.finditer(q)]
    best = intent[0]
    best_dist = None
    for pos, t in intent:
        dist = min((abs(pos - vp) for vp in verb_pos), default=999)
        if best_dist is None or dist < best_dist:
            best_dist, best = dist, (pos, t)
    return best[1]

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

    # 1. Operación (transaccional; inversión + "fácil de arrendar" NO fuerza Arriendo)
    op_detectada = _detectar_operacion(texto)
    if op_detectada:
        filtros["operacion"] = op_detectada

    # 2. Tipo de propiedad (intención real, no menciones incidentales)
    tipo_detectado = _detectar_tipo(texto)
    if tipo_detectado:
        filtros["tipo"] = tipo_detectado

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

    # 4. Dormitorios (exacto si el usuario pide "N dormitorios"; mínimo solo si la
    #    palabra de dirección "desde/al menos/más de" está ADYACENTE al número)
    m = _RE_DORMS.search(texto)
    if not m:
        m = _RE_DORMS_PALABRA.search(texto)
        if m:
            filtros["dormitorios"] = _NUMEROS_PALABRA.get(m.group(1).lower().split()[0], 1)
    else:
        filtros["dormitorios"] = int(m.group(1))
    if filtros.get("dormitorios"):
        es_min = bool(_RE_DORMS_MIN.search(texto) or _RE_DORMS_PALABRA_MIN.search(texto))
        filtros["dormitorios_exacto"] = not es_min
        # Diagnóstico: conflicto de restricciones (ej: "exactamente 2 ... por lo
        # menos 3 dormitorios"). Flag informativo; no cambia el query Mongo.
        m_exacto = _RE_DORMS.search(texto) or _RE_DORMS_PALABRA.search(texto)
        m_min = _RE_DORMS_MIN.search(texto) or _RE_DORMS_PALABRA_MIN.search(texto)
        if m_exacto and m_min:
            v_exacto = int(m_exacto.group(1)) if m_exacto.group(1).isdigit() else \
                _NUMEROS_PALABRA.get(m_exacto.group(1).lower().split()[0], 1)
            v_min = int(m_min.group(1)) if m_min.group(1).isdigit() else \
                _NUMEROS_PALABRA.get(m_min.group(1).lower().split()[0], 1)
            if v_exacto != v_min:
                filtros["conflicto_dormitorios"] = True

    # 5. Baños
    m = _RE_BANOS.search(texto)
    if not m:
        m = _RE_BANOS_PALABRA.search(texto)
        if m:
            filtros["banos"] = _NUMEROS_PALABRA.get(m.group(1).lower().split()[0], 1)
    else:
        filtros["banos"] = int(m.group(1))
    if filtros.get("banos"):
        es_min = bool(_RE_BANOS_MIN.search(texto) or _RE_BANOS_PALABRA_MIN.search(texto))
        filtros["banos_exacto"] = not es_min

    # 5b. Estacionamientos (dígitos + palabras, exacto/mínimo, "sin estacionamiento")
    estac = _extraer_estacionamientos(texto)
    if estac:
        filtros.update(estac)

    # 5c. Bodegas (atributo, no tipo; exacto/mínimo; "sin bodega")
    bodegas = _extraer_bodegas(texto)
    if bodegas:
        filtros.update(bodegas)

    # 5d. Piso (exacto / desde / hasta)
    piso = _extraer_piso(texto)
    if piso:
        filtros.update(piso)

    # 5e. Orientación (HARD solo con contexto explícito)
    orientacion = _extraer_orientacion(texto)
    if orientacion:
        filtros["orientacion"] = orientacion

    # 5f. Comunas similares / vecinos desde lenguaje natural
    if _detectar_vecinos(texto):
        filtros["include_neighbors"] = True

    # 6. Superficie mínima
    m = _RE_M2.search(texto)
    if m:
        filtros["m2_utiles"] = int(m.group(1))

    # 7. Precio (UF o CLP) — los montos de gastos comunes se MASCAN antes
    #    para que nunca se capturen como precio de la propiedad.
    gc_filtros, gc_spans = _extraer_gastos_comunes(texto)
    if gc_filtros:
        filtros.update(gc_filtros)
    texto_precio = _enmascarar(texto, gc_spans)
    precio = _extraer_precio(texto_precio)
    tiene_uf = bool(precio.get("precio_uf_min") or precio.get("precio_uf_max"))
    tiene_clp = bool(precio.get("precio_clp_min") or precio.get("precio_clp_max"))
    if tiene_uf:
        filtros.update({k: v for k, v in precio.items() if k.startswith("precio_uf")})
        # UF es la unidad canónica de ProCasa para venta; si además se mencionó
        # CLP de forma SEPARADA, se marca el presupuesto como dual (no se aplican
        # ambos filtros simultáneamente para no anular la búsqueda).
        if tiene_clp and not precio.get("_overlap_clp_uf"):
            filtros["presupuesto_dual_moneda"] = True
    elif tiene_clp:
        filtros.update({k: v for k, v in precio.items() if k.startswith("precio_clp")})

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
        filtros["estacionamientos_exacto"] = False
    if not (filtros.get("precio_uf_max") or filtros.get("precio_clp_max")
            or filtros.get("precio_uf_min") or filtros.get("precio_clp_min")) and criterios.get("presupuesto"):
        presupuesto = safe_int_conversion(criterios["presupuesto"])
        if presupuesto > 0:
            if presupuesto < 30000:
                filtros["precio_uf_max"] = presupuesto
            else:
                filtros["precio_clp_max"] = presupuesto
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

    # Orientación (HARD): solo cuando fue solicitada explícitamente.
    # Misma semántica de componentes que _post_filtrar_atributos:
    # Norte → Norte, Nor-Oriente, Nor-Poniente (nunca Sur/Oriente puro).
    if filtros.get("orientacion"):
        pat_ori = _regex_orientacion_compatible(filtros["orientacion"])
        if pat_ori:
            clauses.append({"$or": [
                {"caracteristicas.orientacion": pat_ori},
                {"orientacion": pat_ori},
            ]})

    if filtros.get("estacionamientos") is not None:
        e_val = filtros["estacionamientos"]
        if filtros.get("estacionamientos_exacto"):
            clauses.append({"$or": [
                {"caracteristicas.estacionamientos": e_val},
                {"caracteristicas.estacionamientos_cubiertos": e_val},
                {"caracteristicas.estacionamientos_descubiertos": e_val},
                {"estacionamientos": e_val},
            ]})
        else:
            clauses.append({"$or": [
                {"caracteristicas.estacionamientos": {"$gte": e_val}},
                {"caracteristicas.estacionamientos_cubiertos": {"$gte": e_val}},
                {"caracteristicas.estacionamientos_descubiertos": {"$gte": e_val}},
                {"estacionamientos": {"$gte": e_val}},
            ]})

    # Bodegas (atributo): exacto por defecto; mínimo solo con dirección.
    # Guard de dato anómalo bodegas=319: se limita a cantidades razonables (<=30).
    if filtros.get("bodegas") is not None:
        b_val = filtros["bodegas"]
        if filtros.get("bodegas_exacto"):
            clauses.append({"$or": [
                {"$and": [{"caracteristicas.bodegas": b_val}, {"caracteristicas.bodegas": {"$lte": 30}}]},
                {"$and": [{"bodegas": b_val}, {"bodegas": {"$lte": 30}}]},
            ]})
        else:
            clauses.append({"$or": [
                {"$and": [{"caracteristicas.bodegas": {"$gte": b_val}}, {"caracteristicas.bodegas": {"$lte": 30}}]},
                {"$and": [{"bodegas": {"$gte": b_val}}, {"bodegas": {"$lte": 30}}]},
            ]})

    # Piso (exacto / desde / hasta). Dato desconocido no confirma.
    if filtros.get("piso") is not None:
        p_val = filtros["piso"]
        p_dir = filtros.get("piso_dir") or "exacto"
        if p_dir == "min":
            p_f = {"$gte": p_val}
        elif p_dir == "max":
            p_f = {"$lte": p_val}
        else:
            p_f = {"$eq": p_val}
        clauses.append({"$or": [
            {"caracteristicas.piso": p_f},
            {"piso": p_f},
        ]})

    # Gastos comunes (HARD): restricción numérica; dato desconocido no
    # confirma. Guard de datos anómalos (gc=1, gc=55): piso de saneza $1.000.
    if filtros.get("gastos_comunes_max") is not None or filtros.get("gastos_comunes_min") is not None:
        gc_f = {"$gte": 1000}
        if filtros.get("gastos_comunes_max") is not None:
            gc_f["$lte"] = filtros["gastos_comunes_max"]
        if filtros.get("gastos_comunes_min") is not None:
            gc_f["$gte"] = max(filtros["gastos_comunes_min"], 1000)
        clauses.append({"$or": [
            {"tipo_operacion.gastos_comunes": gc_f},
            {"caracteristicas.gastos_comunes": gc_f},
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
        # Tolerancia de negocio +15% sobre presupuestos máximos escritos.
        if filtros.get("precio_uf_max"):
            precio_uf["$lte"] = filtros["precio_uf_max"] * 1.15
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
        # Tolerancia de negocio +15% sobre presupuestos máximos escritos.
        if filtros.get("precio_clp_max"):
            precio_clp["$lte"] = filtros["precio_clp_max"] * 1.15
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

    # "comunas similares / sectores cercanos" en lenguaje natural activa el
    # fallback geográfico (siempre como fallback, nunca mezcla inicial).
    if filtros.get("include_neighbors"):
        include_neighbors = True

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
        # POST-FILTRO de atributos verificables (orientación, GC, bodegas,
        # estacionamientos, piso): fuente de verdad. Una propiedad sin dato o
        # incompatible NO puede superar el filtro por similaridad.
        candidatos = _post_filtrar_atributos(candidatos, filtros)
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

        # Capa de preferencias negativas (SOFT, determinística): penaliza
        # propiedades cuyo texto contiene el término negado salvo contexto positivo.
        scored = _aplicar_penalizaciones_negativas(scored, query_text)

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
