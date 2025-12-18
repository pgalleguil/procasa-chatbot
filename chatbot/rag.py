# chatbot/rag.py
import logging
import re
from typing import List, Dict, Optional
from config import Config
from .storage import get_db
from .utils import safe_int_conversion

logger = logging.getLogger(__name__)

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
    query = {}
    
    # 1. Operación (Obligatorio idealmente)
    op = normalizar_criterio("operacion", criterios.get("operacion"))
    if op: query["operacion"] = op

    # 2. Tipo de propiedad
    tipo = normalizar_criterio("tipo", criterios.get("tipo"))
    if tipo: query["tipo"] = tipo

    # 3. Comuna (búsqueda flexible)
    comuna = criterios.get("comuna")
    if comuna:
        query["comuna"] = {"$regex": comuna, "$options": "i"}

    # 4. Precio (Rango inteligente)
    presupuesto = safe_int_conversion(criterios.get("presupuesto"))
    if presupuesto > 0:
        # Si es bajo (ej. 5000), asumimos UF. Si es alto (ej. 100.000.000), CLP.
        if presupuesto < 30000:
            query["precio_uf"] = {"$lte": presupuesto * 1.2} # +20% margen
        else:
            query["precio_clp"] = {"$lte": presupuesto * 1.2}

    # 5. Dormitorios (mínimo)
    dorms = safe_int_conversion(criterios.get("dormitorios"))
    if dorms > 0:
        query["dormitorios"] = {"$gte": dorms}

    return query

def buscar_propiedades(criterios: Dict, exclude_codes: List[str] = None, limit: int = 3) -> List[Dict]:
    """
    Ejecuta la búsqueda en MongoDB 'universo_obelix'.
    exclude_codes: Lista de códigos a NO mostrar porque ya se vieron.
    limit: Máximo estricto (default 3).
    """
    db = get_db()
    collection = db[Config.COLLECTION_NAME]
    
    query = construir_query(criterios)
    
    # AGREGADO: Exclusión de propiedades ya vistas
    if exclude_codes:
        query["codigo"] = {"$nin": exclude_codes}

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
        cursor = collection.find(query, projection).sort("precio_uf", 1).limit(limit)
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
            f"  Amenities/Desc: {str(p.get('descripcion_clean', ''))[:250]}...\n" # Recortamos para no saturar token
            f"  Link: https://www.procasa.cl/{p.get('codigo')}\n\n"
        )
    texto += "--- FIN LISTADO ---"
    return texto