import pandas as pd
from pymongo import MongoClient, UpdateOne
import math
import sys
import os
import glob
import re
import argparse
from datetime import datetime
from tqdm import tqdm

from config import Config
from sentence_transformers import SentenceTransformer

# --- CONFIGURACIÓN ML ---
sys.path.append("c:\\Users\\pgall\\Desktop\\Python\\vectores\\texto")
try:
    from limpieza_descripcion import clean_text
except ImportError:
    print("WARNING: No se pudo importar limpieza_descripcion.py")
    clean_text = lambda x: ""

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
embedding_model = None

def get_embedding_model():
    global embedding_model
    if embedding_model is None:
        print("\nCargando modelo NLP para embeddings (primera vez puede demorar)...")
        embedding_model = SentenceTransformer(EMBEDDING_MODEL)
    return embedding_model


# ─── UTILIDADES ────────────────────────────────────────────────────────────────

def cv(v):
    """clean_value: retorna None si el valor está vacío o NaN"""
    if v is None: return None
    if isinstance(v, float) and math.isnan(v): return None
    s = str(v).strip()
    return s if s not in ("", "nan", "None", "-") else None

def normalize_for_compare(val):
    if val is None:
        return None
        
    s = str(val).strip()
    # Normalize multiple internal spaces to a single space
    s = re.sub(r'\s+', ' ', s)
    s = s.lower()
    
    # Handle empties and null equivalents
    if s in ("", "-", "nan", "none", "null"):
        return None
        
    # Financial and numeric cleanup
    # Remove $, UF, CLP, % and their surrounding spaces
    s_clean = re.sub(r'\$|uf|clp|%', '', s).strip()
    
    # Remove thousand separators (.) and commas (,) if it looks like a number
    if re.match(r'^[\d.,\s]+$', s_clean):
        s_clean = s_clean.replace('.', '').replace(',', '').strip()
    
    # Cast to numeric if possible
    try:
        if '.' in s_clean:
            return float(s_clean)
        else:
            return int(s_clean)
    except ValueError:
        pass
        
    return s_clean if s_clean else None

def format_name(v):
    s = cv(v)
    return s.title() if s else None

def format_phone(v):
    s = cv(v)
    if not s: return None
    digits = "".join(filter(str.isdigit, s))
    if len(digits) == 8:  return "569" + digits
    if len(digits) == 9 and digits.startswith("9"): return "56" + digits
    if len(digits) == 11 and digits.startswith("569"): return digits
    return digits or None

def slugify_portal(name):
    """Convierte 'Portal Inmobiliario - Mercado Libre' a 'portal_inmobiliario'"""
    if not name: return "desconocido"
    s = str(name).lower()
    if "portal inmobiliario" in s: return "portal_inmobiliario"
    if "yapo" in s: return "yapo"
    # Genérico: todo lo que no sea letra o número se vuelve _
    s = re.sub(r'[^a-z0-9]+', '_', s).strip('_')
    return s


# ─── PARSER ARCHIVO 1: propiedades_*.xlsx ─────────────────────────────────────

def parse_propiedades_row(row):
    """
    Mapa por índice de columna (header=5, formato Convecta semanal)
    """
    vals = list(row)
    codigo = cv(vals[1] if len(vals) > 1 else None)
    if not codigo: return None

    en_venta = str(cv(vals[7]) or "").strip()
    estado_venta = str(cv(vals[8]) or "").strip().lower()
    en_arriendo = str(cv(vals[15]) or "").strip()
    estado_arriendo = str(cv(vals[16]) or "").strip().lower()

    if en_venta == "Sí":
        operacion = "Venta"
        disponible = (estado_venta == "activa")
        divisa = cv(vals[9]); precio_ppal = cv(vals[10]); precio_uf = cv(vals[11])
        precio_clp = cv(vals[12]); por_m2 = cv(vals[13]); precio_tas = cv(vals[14])
    elif en_arriendo == "Sí":
        operacion = "Arriendo"
        disponible = (estado_arriendo == "activa")
        divisa = cv(vals[17]); precio_ppal = cv(vals[18]); precio_uf = cv(vals[19])
        precio_clp = cv(vals[20]); por_m2 = cv(vals[21]); precio_tas = cv(vals[22])
    else:
        operacion = "Desconocida"
        disponible = False
        divisa = precio_ppal = precio_uf = precio_clp = por_m2 = precio_tas = None

    desc_raw = cv(vals[57]) if len(vals) > 57 else None
    desc_clean = clean_text(str(desc_raw)) if desc_raw else ""

    doc = {
        "codigo": codigo,
        "source": "convecta",
        "rol": cv(vals[2]),
        "fecha_incorporacion": cv(vals[3]),
        "ejecutivo": cv(vals[4]),
        "email_ejecutivo": cv(vals[5]),
        "oficina": cv(vals[6]),
        "operacion": operacion,
        "disponible": disponible,
        "divisa": divisa,
        "precio_ppal": precio_ppal,
        "precio_uf": precio_uf,
        "precio_clp": precio_clp,
        "por_m2": por_m2,
        "precio_tasacion": precio_tas,
        "tipo": cv(vals[23]),
        "tipo_2": cv(vals[24]),
        "tipo_3": cv(vals[25]),
        "tipo_4": cv(vals[26]),
        "calle_referencia": cv(vals[27]),
        "direccion_propietario": cv(vals[28]),
        "n": cv(vals[29]),
        "unidad": cv(vals[30]),
        "letra": cv(vals[31]),
        "etapa": cv(vals[32]),
        "sector": cv(vals[33]),
        "comuna": cv(vals[34]),
        "region": cv(vals[35]),
        "latitud": cv(vals[36]),
        "longitud": cv(vals[37]),
        "mapa_web": cv(vals[38]),
        "mapa_portales": cv(vals[39]),
        "dormitorios": cv(vals[40]),
        "banos": cv(vals[41]),
        "privados": cv(vals[42]),
        "m2_construida": cv(vals[43]),
        "m2_utiles": cv(vals[44]),
        "m2_terreno": cv(vals[45]),
        "m2_total": cv(vals[46]),
        "m2_terraza": cv(vals[47]),
        "estacionamientos": cv(vals[48]),
        "bodega": cv(vals[49]),
        "letrero": cv(vals[50]),
        "web_portales": cv(vals[51]),
        "destacada_web": cv(vals[52]),
        "exclusiva": cv(vals[53]),
        "gastos_comunes": cv(vals[54]),
        "ultima_actualizacion": cv(vals[55]),
        "ordenes_visitas": cv(vals[56]),
        "descripcion": desc_raw,
        "descripcion_clean": desc_clean,
        "forma_visitar": cv(vals[58]),
        "observaciones_internas": cv(vals[59]),
        "rut_propietario": str(cv(vals[60]) or "").replace(".", "").replace("-", "").strip() or None if len(vals) > 60 else None,
        "nombre_propietario": format_name(vals[61]),
        "email_propietario": cv(vals[62]),
        "movil_propietario": format_phone(vals[63]),
        "movil_propietario_2": format_phone(vals[64]),
        "movil_propietario_3": format_phone(vals[65]),
        "duplicada": cv(vals[66]),
        "codigo_original": cv(vals[67]),
        "fecha_duplicacion": cv(vals[68]) if len(vals) > 68 else None,
    }
    return {k: v for k, v in doc.items() if v is not None and v != ""}


# ─── PARSER ARCHIVO 3: prop_*.xls (Red Procasa - Tiempo Real) ───────────────

def parse_prop_network_row(vals):
    """Parser para el archivo de red Procasa (prop_*.xls)"""
    codigo_raw = cv(vals[2] if len(vals) > 2 else None)
    if not codigo_raw: return None
    if str(codigo_raw).lower() in ("código", "codigo", "c\u00f3digo", "#"): return None
    codigo = str(codigo_raw).replace(".", "").strip()

    oficina = cv(vals[7] if len(vals) > 7 else None)
    en_venta = str(cv(vals[8] if len(vals) > 8 else None) or "").strip().upper()
    en_arriendo = str(cv(vals[13] if len(vals) > 13 else None) or "").strip().upper()
    
    estado = str(cv(vals[4] if len(vals) > 4 else None) or "").strip().lower()
    disponible = (estado == "activa")

    if en_venta == "SI":
        operacion = "Venta"
        divisa = cv(vals[9]); precio_ppal = cv(vals[10]); precio_uf = cv(vals[11])
        precio_clp = cv(vals[12])
    elif en_arriendo == "SI":
        operacion = "Arriendo"
        divisa = cv(vals[14]); precio_ppal = cv(vals[15]); precio_uf = cv(vals[16])
        precio_clp = cv(vals[17])
    else:
        operacion = "Desconocida"
        divisa = precio_ppal = precio_uf = precio_clp = None

    doc = {
        "codigo": codigo,
        "source": "red_procasa",
        "rol": cv(vals[3]),
        "fecha_incorporacion": cv(vals[5]),
        "ejecutivo": cv(vals[6]),
        "oficina": oficina,
        "operacion": operacion,
        "disponible": disponible,
        "divisa": divisa,
        "precio_ppal": precio_ppal,
        "precio_uf": precio_uf,
        "precio_clp": precio_clp,
        "tipo": cv(vals[18]),
        "calle_referencia": cv(vals[22]),
        "sector": cv(vals[28]),
        "comuna": cv(vals[29]),
        "region": cv(vals[30]),
        "latitud": cv(vals[31]),
        "longitud": cv(vals[32]),
        "mapa_web": cv(vals[33]),
        "mapa_portales": cv(vals[34]),
        "dormitorios": cv(vals[35]),
        "banos": cv(vals[36]),
        "m2_construida": cv(vals[37]),
        "m2_utiles": cv(vals[38]),
        "m2_terreno": cv(vals[39]),
        "m2_total": cv(vals[40]),
        "letrero": cv(vals[41]),
        "web_portales": cv(vals[42]),
        "destacada_web": cv(vals[43]),
        "exclusiva": cv(vals[44]),
        "gastos_comunes": cv(vals[45]),
        "ultima_actualizacion": cv(vals[46]),
        "codigo_pi": cv(vals[56]),
    }
    return {k: v for k, v in doc.items() if v is not None and v != ""}


# ─── PUBLICACIONES MAPPER ──────────────────────────────────────────────────────

def map_publicaciones(file_path):
    pub_dict = {}
    if not file_path:
        return pub_dict
    print(f"Procesando publicaciones: {file_path}")
    df_pub = pd.read_excel(file_path, header=5)
    for _, row in df_pub.iterrows():
        try:
            vals = list(row)
            codigo = cv(vals[1] if len(vals) > 1 else None)
            if not codigo: continue
            
            portal_raw = cv(vals[7])
            portal_key = slugify_portal(portal_raw)
            
            pub = {
                "portal_name": portal_raw,
                "codigo_pi": cv(vals[8]),
                "url_pi": cv(vals[9]),
                "calidad": cv(vals[10])
            }
            # Guardamos como objeto mapeado por portal_key (para acceso directo en el CRM)
            pub_dict.setdefault(codigo, {})[portal_key] = pub
        except Exception:
            pass
    return pub_dict


# ─── DESCUBRIMIENTO DINÁMICO ───────────────────────────────────────────────────

def get_latest_file(prefix, ext="xlsx"):
    paths = [
        os.path.join(os.path.expanduser("~"), "Downloads"),
        "c:\\Users\\pgall\\Desktop\\Python\\ChatBot_v4_Grok"
    ]
    for path in paths:
        files = glob.glob(os.path.join(path, f"{prefix}*.{ext}"))
        if files:
            return max(files, key=os.path.getmtime)
    return None

TRACKED_FIELDS = [
    "precio_uf", "precio_clp", "ejecutivo", "nombre_propietario",
    "movil_propietario", "movil_propietario_2", "movil_propietario_3",
    "email_propietario", "disponible"
]


# ─── UPSERT CON AUDITORÍA ─────────────────────────────────────────────────────

def upsert_doc(coll, doc, dict_pub, model_nlp=None, oficina_target="PROCASA SUCRE"):
    codigo = doc.get("codigo")
    existing = coll.find_one({"codigo": codigo})

    # Fusionar publicaciones (Overlay)
    # Mantenemos las existentes si ya hay datos de scraping previos
    publicaciones = existing.get("publicaciones", {}) if existing else {}
    if codigo in dict_pub:
        # El archivo oficial de publicaciones añade/sobrescribe sus llaves
        publicaciones.update(dict_pub[codigo])
    
    if publicaciones:
        doc["publicaciones"] = publicaciones

    # Auditoria de historial
    doc["historial_cambios"] = list(existing.get("historial_cambios", [])) if existing else []

    cambios_count = 0
    if existing:
        for field in TRACKED_FIELDS:
            old_val = existing.get(field)
            new_val = doc.get(field)
            
            norm_old = normalize_for_compare(old_val)
            norm_new = normalize_for_compare(new_val)
            
            if norm_old != norm_new:
                # Evitar registrar si el último cambio es idéntico a lo que vamos a registrar
                skip = False
                if doc["historial_cambios"]:
                    last_change = doc["historial_cambios"][-1]
                    if (last_change.get("campo") == field and 
                        normalize_for_compare(last_change.get("valor_anterior")) == norm_old and 
                        normalize_for_compare(last_change.get("valor_nuevo")) == norm_new):
                        skip = True
                
                if not skip:
                    doc["historial_cambios"].append({
                        "fecha": datetime.now().isoformat(),
                        "campo": field,
                        "valor_anterior": old_val,
                        "valor_nuevo": new_val
                    })
                    cambios_count += 1
                    if field == "ejecutivo" and doc.get("oficina") == "PROCASA SUCRE":
                        print(f" [CAMBIO EJECUTIVO - PROCASA SUCRE] Código {codigo}: {old_val} -> {new_val}")

    # Snapshot explícito de cambios críticos para campañas (solo oficina target)
    oficina_actual = str(doc.get("oficina") or existing.get("oficina") or "").strip()
    if existing and oficina_actual == oficina_target:
        old_precio = existing.get("precio_uf")
        new_precio = doc.get("precio_uf", existing.get("precio_uf"))
        old_exec = existing.get("ejecutivo")
        new_exec = doc.get("ejecutivo", existing.get("ejecutivo"))

        if str(old_precio).strip() != str(new_precio).strip():
            doc["precio_uf_anterior"] = old_precio
            doc["precio_uf_cambio_at"] = datetime.now().isoformat()
            doc["precio_uf_cambio_origen"] = "sync_convecta_master"
        if str(old_exec).strip().lower() != str(new_exec).strip().lower():
            doc["ejecutivo_anterior"] = old_exec
            doc["ejecutivo_cambio_at"] = datetime.now().isoformat()
            doc["ejecutivo_cambio_origen"] = "sync_convecta_master"

    # NLP Vectors
    vector_gen = False
    if doc.get("disponible") and doc.get("descripcion_clean"):
        needs_new_vector = not existing or \
            existing.get("descripcion_clean", "") != doc.get("descripcion_clean", "") or \
            "vector_descripcion" not in existing
        if needs_new_vector and model_nlp:
            try:
                doc["vector_descripcion"] = model_nlp.encode(doc["descripcion_clean"]).tolist()
                vector_gen = True
            except Exception:
                pass

    result = coll.update_one({"codigo": codigo}, {"$set": doc}, upsert=True)
    nuevo = bool(result.upserted_id)
    actualizado = not nuevo and result.modified_count > 0

    return nuevo, actualizado, cambios_count, vector_gen


# ─── MASTER SYNC ─────────────────────────────────────────────────────────────

def master_sync(oficina_target="PROCASA SUCRE"):
    file_net  = get_latest_file("prop_", "xls")           # Tiémp real (Red)
    file_prop = get_latest_file("propiedades_", "xlsx")    # Semanal (Office)
    file_pub  = get_latest_file("publicaciones_", "xlsx")  # Portales

    if not file_net:
        print("ERROR: No se encontro archivo prop_*.xls en Downloads.")
        return

    print(f"Archivo Red Procasa (Real-Time): {file_net}")
    print(f"Archivo Nuestra Oficina (Semanal): {file_prop or 'No encontrado'}")
    print(f"Archivo Publicaciones Portales: {file_pub or 'No encontrado'}")
    print(f"Oficina target auditoría crítica: {oficina_target}")

    dict_pub = map_publicaciones(file_pub)
    
    # 1. Pre-carga de modelo NLP (para evitar stalls en la barra de progreso)
    model_nlp = None
    if file_prop:
        model_nlp = get_embedding_model()

    client = MongoClient(Config.MONGO_URI)
    db = client[Config.DB_NAME]
    coll = db["universo_cartera"]

    # Indices
    coll.create_index("codigo", unique=True)
    coll.create_index("rol")
    coll.create_index("source")

    # Set para trackear qué códigos encontramos en los archivos nuevos
    codigos_encontrados_en_excel = set()
    nuevos = actualizados = vectores = cambios_audit = 0

    # ── FASE 1: prop_ (Red Procasa completa, TIEMPO REAL) ──────────────────────
    print("\nFASE 1 [TIEMPO REAL]: Sincronizando Red Procasa completa...")
    df_net = pd.read_excel(file_net, header=5, engine="xlrd")
    for idx, row in tqdm(df_net.iterrows(), total=len(df_net), desc="prop_ red"):
        doc = parse_prop_network_row(list(row))
        if not doc: continue
        cod_str = str(doc.get("codigo")).strip()
        codigos_encontrados_en_excel.add(cod_str)
        
        n, a, c, v = upsert_doc(coll, doc, dict_pub, None, oficina_target=oficina_target)
        nuevos += n; actualizados += a; cambios_audit += c; vectores += v

    # ── FASE 2: propiedades_ (Nuestra oficina SUCRE, semanal) ──────────────────
    if file_prop:
        print("\nFASE 2 [SEMANAL]: Enriqueciendo con datos de nuestra oficina...")
        df_prop = pd.read_excel(file_prop, header=5)
        df_prop = df_prop.dropna(how="all", axis=1)
        for idx, row in tqdm(df_prop.iterrows(), total=len(df_prop), desc="propiedades_"):
            doc = parse_propiedades_row(list(row))
            if not doc: continue
            cod_str = str(doc.get("codigo")).strip()
            codigos_encontrados_en_excel.add(cod_str)
            
            n, a, c, v = upsert_doc(coll, doc, dict_pub, model_nlp, oficina_target=oficina_target)
            nuevos += n; actualizados += a; cambios_audit += c; vectores += v
    else:
        print("\nFASE 2: Sin archivo propiedades_ (se usaran datos de la red para esta oficina).")

    # ── FASE 3: LIMPIEZA DE BAJAS (Sincronización con realidad 446) ────────────
    print(f"\nFASE 3 [LIMPIEZA]: Verificando propiedades que ya no están en los archivos...")
    
    # Buscamos propiedades que el sistema cree disponibles pero que no vinieron en el Excel
    query_bajas = {
        "oficina": oficina_target,
        "disponible": True,
        "codigo": {"$nin": list(codigos_encontrados_en_excel)}
    }
    
    bajas_count = coll.count_documents(query_bajas)
    if bajas_count > 0:
        coll.update_many(
            query_bajas, 
            {"$set": {"disponible": False, "activa_obelix": False, "fecha_baja_automatica": datetime.now().isoformat()}}
        )
        print(f"   -> SE DETECTARON {bajas_count} BAJAS. (Marcadas como disponibles: False)")
    else:
        print("   -> No se detectaron bajas nuevas. Inventario al día.")

    print(f"\n--- REPORTE DE SINCRONIZACION ---")
    print(f"Propiedades Nuevas: {nuevos}")
    print(f"Propiedades Actualizadas: {actualizados}")
    print(f"Propiedades Dadas de Baja: {bajas_count}")
    print(f"Cambios Auditados en Historial: {cambios_audit}")
    print(f"Vectores NLP Generados: {vectores}")
    print(f"Total en coleccion: {coll.count_documents({})}")
    print(f"Disponibles Reales ({oficina_target}): {coll.count_documents({'oficina': oficina_target, 'disponible': True})}")
    print("Proceso finalizado correctamente.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--oficina-target", default="PROCASA SUCRE", help="Oficina para snapshots críticos y bajas automáticas")
    args = ap.parse_args()
    master_sync(oficina_target=args.oficina_target)
