# rag.py
from pymongo import MongoClient
from config import Config
from typing import List, Dict, Any
import random

config = Config()
client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]
propiedades = db[config.COLLECTION_NAME]  # universo_obelix

def buscar_propiedades(criteria: Dict[str, Any], limit: int = 3, ya_vistas: List[str] = None) -> List[Dict[str, Any]]:
    ya_vistas = ya_vistas or []

    # ================================
    # QUERY BASE
    # ================================
    base_query = {
        "operacion": criteria["operacion"],
        "tipo": criteria["tipo"],
        "disponible": True
    }

    # SOPORTE MÚLTIPLES COMUNAS
    if "comuna" in criteria:
        if isinstance(criteria["comuna"], list) and criteria["comuna"]:
            base_query["comuna"] = {"$in": criteria["comuna"]}
        elif criteria["comuna"]:
            base_query["comuna"] = criteria["comuna"]

    # Excluir ya vistas
    if ya_vistas:
        base_query["codigo"] = {"$nin": ya_vistas}

    # ================================
    # FILTROS ADICIONALES
    # ================================
    if dorm := criteria.get("dormitorios"):
        try:
            val = int(float(dorm)) if isinstance(dorm, (str, float)) else int(dorm)
            if val > 0:
                base_query["dormitorios"] = {"$gte": val}
        except:
            pass

    if banos := criteria.get("banos"):
        try:
            val = int(float(banos))
            if val > 0:
                base_query["banos"] = {"$gte": val}
        except:
            pass

    if estac := criteria.get("estacionamientos"):
        try:
            val = int(float(estac))
            if val > 0:
                base_query["estacionamientos"] = {"$gte": val}
        except:
            pass

    if precio_uf := criteria.get("precio_uf"):
        if isinstance(precio_uf, dict):
            base_query["precio_uf"] = precio_uf
        elif isinstance(precio_uf, (int, float)):
            base_query["precio_uf"] = {"$lte": float(precio_uf) * 1.15}

    if precio_clp := criteria.get("precio_clp"):
        if isinstance(precio_clp, dict):
            base_query["precio_clp"] = precio_clp
        elif isinstance(precio_clp, (int, float)):
            base_query["precio_clp"] = {"$lte": float(precio_clp) * 1.15}

    print(f"[RAG] Query final: {base_query}")

    # ================================
    # 1. PRIORIDAD: Nuestras propiedades
    # ================================
    nuestras_query = base_query.copy()
    nuestras_query["oficina"] = config.PRIORITY_OFICINA

    nuestras = list(propiedades.find(nuestras_query).sort("dormitorios", 1))
    random.shuffle(nuestras)
    nuestras = nuestras[:limit]

    print(f"[RAG] Propiedades NUESTRAS encontradas: {len(nuestras)}")

    # ================================
    # 2. Completar con externas
    # ================================
    resultado = nuestras.copy()
    faltan = limit - len(resultado)

    if faltan > 0:
        externas_query = base_query.copy()
        externas_query["oficina"] = {"$ne": config.PRIORITY_OFICINA}
        externas = list(propiedades.find(externas_query).sort("dormitorios", 1))
        random.shuffle(externas)
        resultado += externas[:faltan]

    print(f"[RAG] TOTAL propiedades devueltas: {len(resultado)}")
    for p in resultado:
        print(f"   → {p.get('codigo')} | {p.get('comuna')} | {p.get('dormitorios', '?')} dorm | {p.get('oficina', 'Externa')}")

    return resultado


def formatear_propiedad(prop: Dict[str, Any]) -> str:
    codigo = prop.get("codigo", "SIN-CODIGO")
    precio_uf = prop.get("precio_uf", prop.get("precio", "?"))
    precio_clp = prop.get("precio_clp")
    clp_text = f" ({precio_clp:,} CLP)" if precio_clp else ""

    dorm = prop.get("dormitorios", "?")
    banos = prop.get("banos", "?")
    m2 = prop.get("m2_utiles") or prop.get("superficie_util", "?")

    return (f"• {prop.get('tipo','Propiedad')} en {prop.get('comuna','?')}\n"
            f"  {dorm} dorm. | {banos} baños | {m2} m² útiles\n"
            f"  Precio: {precio_uf} UF{clp_text}\n"
            f"  Código: {codigo}\n"
            f"  🔗 https://www.procasa.cl/{codigo}")