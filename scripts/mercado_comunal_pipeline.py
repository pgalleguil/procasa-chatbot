#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pipeline de inteligencia de mercado comunal (comuna + tipo propiedad).

- Lee PDFs de analisis comunal
- Normaliza nombre archivo corrupto
- Procesa solo casa/departamento
- Extrae indicadores de mercado
- Upsert en coleccion mercado_comunal
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pymongo import ASCENDING
from pymongo.collection import Collection
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db
from scripts.comercial_normalization import normalize_comuna_tipo

logger = logging.getLogger("mercado_comunal_pipeline")


# -------------------------
# Helpers
# -------------------------
def normalize_ws(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def parse_number(raw: str) -> float:
    if not raw:
        return 0.0
    raw = raw.strip().replace(" ", "")
    raw = re.sub(r"[^\d,\.\-+]", "", raw)
    if not raw:
        return 0.0
    if "." in raw and "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    elif raw.count(".") == 1 and "," not in raw:
        l, r = raw.split(".", 1)
        if len(r) == 3 and l.replace("+", "").replace("-", "").isdigit():
            raw = l + r
    elif "," in raw and "." not in raw:
        l, r = raw.split(",", 1)
        if len(r) == 3 and l.isdigit():
            raw = l + r
        else:
            raw = l + "." + r
    try:
        return float(raw)
    except ValueError:
        return 0.0


def parse_int(raw: str) -> int:
    return int(round(parse_number(raw)))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def pct_change(first: float, last: float) -> float:
    if first == 0:
        return 0.0
    return ((last - first) / first) * 100.0


# -------------------------
# Filename normalization
# -------------------------
FILENAME_REPLACEMENTS = {
    "via_del_mar": "viña_del_mar",
    "valparaso": "valparaíso",
    "galpn": "galpón",
    "habitacin": "habitación",
    "concn": "concón",
    "estacin_central": "estación_central",
    "maip": "maipú",
    "pealoln": "peñalolén",
    "uoa": "ñoñoa",
    "hualpn": "hualpén",
    "chilln": "chillán",
    "curic": "curicó",
    "talca": "talca",
    "tom": "tomé",
    "colbn": "colbún",
    "longav": "longaví",
    "ro_claro": "río_claro",
    "san_ramn": "san_ramón",
    "san_joaqun": "san_joaquín",
    "san_jos_de_maipo": "san_josé_de_maipo",
    "huala": "hualañé",
    "alhu": "alhué",
    "licantn": "licantén",
    "caete": "cañete",
    "constitucin": "constitución",
    "mulchn": "mulchén",
    "quilpu": "quilpué",
}

TYPE_NORMALIZATION = {
    "casa_habitación": "casa_habitación",
    "departamento_habitación": "departamento_habitación",
    "casa": "casa",
    "departamento": "departamento",
    "oficina": "oficina",
    "comercial": "comercial",
    "industrial": "industrial",
    "terreno": "terreno",
    "parcela": "parcela",
    "agrícola_forestal": "agrícola_forestal",
    "agricola_forestal": "agrícola_forestal",
    "bodega": "bodega",
    "bodegas": "bodega",
    "estacionamiento": "estacionamiento",
    "estacionamientos": "estacionamiento",
    "galpón": "galpón",
    "galpon": "galpón",
    "unidad_agroeconomica": "unidad_agroeconomica",
    "parcela_agroresidencial": "parcela_agroresidencial",
    "local": "local",
}


def normalize_filename_slug(filename: str) -> str:
    slug = filename.lower().replace(".pdf", "")
    for bad, good in FILENAME_REPLACEMENTS.items():
        slug = slug.replace(bad, good)
    return slug


def parse_comuna_tipo_from_filename(filename: str) -> Tuple[str, str, str]:
    slug = normalize_filename_slug(filename)

    tipo_raw = ""
    comuna_slug = slug
    for maybe in sorted(TYPE_NORMALIZATION.keys(), key=len, reverse=True):
        token = maybe.replace(" ", "_")
        if slug.endswith("_" + token):
            tipo_raw = TYPE_NORMALIZATION.get(maybe, maybe)
            comuna_slug = slug[: -(len(token) + 1)]
            break

    comuna = comuna_slug.replace("_", " ").strip().title()
    return comuna, tipo_raw, slug


def normalize_tipo_propiedad(tipo_raw: str, tipo_pdf: str) -> str:
    tpdf = (tipo_pdf or "").strip().lower()
    traw = (tipo_raw or "").strip().lower()
    t = tpdf if tpdf else traw
    t = t.replace("\x00", "")
    t = re.sub(r"[^a-záéíóúñ_\\-\\s]", " ", t)
    t = re.sub(r"\\s+", " ", t).strip()
    t = t.replace("o cina", "oficina")
    t = t.replace("casa-habitación", "casa_habitación").replace("casa-habitacion", "casa_habitación")
    t = t.replace("departamento-habitación", "departamento_habitación").replace("departamento-habitacion", "departamento_habitación")
    t = t.replace(" ", "_")
    t = TYPE_NORMALIZATION.get(t, t)
    if not t:
        return "Desconocido"
    return t.replace("_", " ").title()


# -------------------------
# PDF extraction
# -------------------------
def extract_pdf_pages_text(pdf_path: Path) -> Tuple[List[str], List[str]]:
    import pdfplumber  # type: ignore

    pages_norm: List[str] = []
    pages_layout: List[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            layout_text = page.extract_text(layout=True) or ""
            pages_layout.append(layout_text)
            pages_norm.append(normalize_ws(layout_text))
    return pages_norm, pages_layout


def find_first(pattern: str, text: str, flags: int = re.IGNORECASE) -> str:
    m = re.search(pattern, text, flags)
    return (m.group(1) or "").strip() if m else ""


def extract_metadata(pages: List[str]) -> Dict[str, str]:
    txt = "\n".join(pages[:2])
    codigo = find_first(r"C[oó]digo\s*:\s*([A-Z0-9\-]+)", txt)
    fecha = find_first(r"Fecha\s*:\s*([0-9]{1,2}[\-/][0-9]{1,2}[\-/][0-9]{2,4})", txt)
    ubicacion = find_first(r"Ubicacion\s*:\s*([^\n]+)", txt)
    tipo = find_first(r"Tipo de propiedad\s*:\s*([^\n]+)", txt)
    return {
        "codigo_informe": ubicacion and codigo or codigo,
        "fecha_reporte": fecha,
        "comuna_pdf": ubicacion,
        "tipo_pdf": tipo,
    }


def extract_page2_market_basics(page2: str) -> Dict[str, Any]:
    # Totales / activas
    venta_total = 0
    venta_activas = 0
    arr_total = 0
    arr_activas = 0

    m_all = re.search(
        r"([\d\.]+)\s*\(([\d\.]+)\s*activas\)\s*([\d\.]+)\s*\(([\d\.]+)\s*activas\)",
        page2,
        re.IGNORECASE,
    )
    if m_all:
        venta_total = parse_int(m_all.group(1))
        venta_activas = parse_int(m_all.group(2))
        arr_total = parse_int(m_all.group(3))
        arr_activas = parse_int(m_all.group(4))

    cap_rate = parse_number(find_first(r"(\d+[\,\.]\d+)\s*%\s*\d+\s*a[ñn]os", page2))
    payback = parse_int(find_first(r"\d+[\,\.]\d+\s*%\s*(\d+)\s*a[ñn]os", page2))

    return {
        "publicaciones_totales": venta_total,
        "publicaciones_activas": venta_activas,
        "publicaciones_arriendo_totales": arr_total,
        "publicaciones_arriendo_activas": arr_activas,
        "cap_rate": cap_rate,
        "payback_anios": payback,
    }


def extract_ufm2_from_page(page_text: str) -> Dict[str, float]:
    out = {
        "uf_m2_publicacion_actual": 0.0,
        "uf_m2_venta_efectiva_actual": 0.0,
        "variacion_uf_m2_12m": 0.0,
        "uf_m2_arriendo_actual": 0.0,
        "variacion_arriendo_12m": 0.0,
    }

    # Bloque venta
    m_block = re.search(
        r"Evoluci[oó]n [^\n]*publicaciones en venta[^\n]*?(.*?)Publicaciones venta Ventas efectivas",
        page_text,
        re.IGNORECASE | re.DOTALL,
    )
    if not m_block:
        m_block = re.search(
            r"Evoluci[oó]n [^\n]*publicaciones en venta[^\n]*?(.*?)(?:¿C[oó]mo leo este gr[aá]fico|Evoluci[oó]n [^\n]*publicaciones en arriendo)", 
            page_text, 
            re.IGNORECASE | re.DOTALL
        )
    
    if m_block:
        block_venta = m_block.group(1)
        points = []
        for line in block_venta.splitlines():
            for m in re.finditer(r"\d+[\,\.]\d+", line):
                val = parse_number(m.group())
                if 5 <= val <= 250:
                    points.append((m.start(), val))
        
        if points:
            points.sort(key=lambda t: t[0])
            grouped = []
            curr_group = [points[0]]
            for p in points[1:]:
                # agrupar puntos que esten en la misma columna (tolerancia de 5 chars)
                if abs(p[0] - curr_group[0][0]) <= 5:
                    curr_group.append(p)
                else:
                    grouped.append(curr_group)
                    curr_group = [p]
            grouped.append(curr_group)
            
            high_series = []
            low_series = []
            for g in grouped:
                g.sort(key=lambda t: t[1], reverse=True)
                if len(g) >= 2:
                    high_series.append(g[0][1])
                    low_series.append(g[-1][1])
                elif len(g) == 1:
                    high_series.append(g[0][1])
            
            if high_series:
                out["uf_m2_publicacion_actual"] = round(high_series[-1], 2)
                out["variacion_uf_m2_12m"] = round(pct_change(high_series[0], high_series[-1]), 2)
            if low_series:
                out["uf_m2_venta_efectiva_actual"] = round(low_series[-1], 2)

    # Bloque arriendo
    m_arr = re.search(r"publicaciones en arriendo(.*?)(?:[\n\r]+P[aá]gina|$)", page_text, re.IGNORECASE | re.DOTALL)
    if m_arr:
        block_arr = m_arr.group(1)
        points = []
        for line in block_arr.splitlines():
            for m in re.finditer(r"\d+[\,\.]\d+", line):
                val = parse_number(m.group())
                if 0 < val < 2:
                    points.append((m.start(), val))
        
        if points:
            points.sort(key=lambda t: t[0])
            grouped = []
            # Filtrar el eje Y que tiene muchos puntos en el mismo X (e.g. x=6 o similar al inicio)
            # Primero agrupar por columna
            curr_group = [points[0]]
            for p in points[1:]:
                if abs(p[0] - curr_group[0][0]) <= 4:
                    curr_group.append(p)
                else:
                    grouped.append(curr_group)
                    curr_group = [p]
            grouped.append(curr_group)
            
            # El eje Y tiene varios valores en la misma columna. La serie temporal tendra 1 o 2 valores maximo por columna.
            filtered_groups = [g for g in grouped if len(g) <= 2]
            
            arr_series = [max(g, key=lambda t: t[1])[1] for g in filtered_groups]
            if arr_series:
                out["uf_m2_arriendo_actual"] = round(arr_series[-1], 3)
                out["variacion_arriendo_12m"] = round(pct_change(arr_series[0], arr_series[-1]), 2)

    return out



def extract_ranges_from_page(page_text: str) -> Dict[str, int]:
    out = {"min_uf": 0, "max_uf": 0}
    m_block = re.search(
        r"Evoluci[oó]n de l[ií]mites seg[uú]n valor publicaciones en venta(.*?)(?:Evoluci[oó]n de l[ií]mites seg[uú]n valor publicaciones en arriendo|P[aá]gina|$)",
        page_text,
        re.IGNORECASE | re.DOTALL,
    )
    block = m_block.group(1) if m_block else page_text
    
    # Excluir texto explicativo para no atrapar números del ejemplo
    if "Cmo leo" in block:
        block = block.split("Cmo leo")[0]
    elif "Cómo leo" in block:
        block = block.split("Cómo leo")[0]

    points = []
    for line in block.splitlines():
        for m in re.finditer(r"\d+[\,\.]\d+", line):
            val = parse_number(m.group())
            if val >= 900:
                points.append((m.start(), val))

    if points:
        points.sort(key=lambda t: t[0])
        grouped = []
        curr_group = [points[0]]
        for p in points[1:]:
            if abs(p[0] - curr_group[0][0]) <= 6:
                curr_group.append(p)
            else:
                grouped.append(curr_group)
                curr_group = [p]
        grouped.append(curr_group)

        # Filtrar eje Y que puede tener varios valores en una sola columna (normalmente x muy bajo)
        filtered_groups = [g for g in grouped if len(g) <= 4]
        
        if filtered_groups:
            last_group = filtered_groups[-1]
            out["max_uf"] = int(round(max(t[1] for t in last_group)))
            out["min_uf"] = int(round(min(t[1] for t in last_group)))

    return out


def extract_top_corredores(page_text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    venta: List[Dict[str, Any]] = []
    arr: List[Dict[str, Any]] = []

    m_v = re.search(r"Top publicaciones de venta(.*?)Top publicaciones de arriendo", page_text, re.IGNORECASE | re.DOTALL)
    m_a = re.search(r"Top publicaciones de arriendo(.*)$", page_text, re.IGNORECASE | re.DOTALL)

    def parse_block(block: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for ln in block.splitlines():
            ln = normalize_ws(ln)
            m = re.match(r"^([A-ZÁÉÍÓÚÑ0-9&\.\-\s]{3,}?)\s+(\d{1,4})$", ln)
            if not m:
                continue
            name = normalize_ws(m.group(1)).title()
            cnt = int(m.group(2))
            if name.lower().startswith("código"):
                continue
            if re.match(r"^[0-9\s]+$", name):
                continue
            if not re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]", name):
                continue
            rows.append({"nombre": name, "publicaciones": cnt})
        # dedupe keep max count
        by_name: Dict[str, int] = {}
        for r in rows:
            by_name[r["nombre"]] = max(by_name.get(r["nombre"], 0), r["publicaciones"])
        out = [{"nombre": k, "publicaciones": v} for k, v in by_name.items()]
        out.sort(key=lambda x: x["publicaciones"], reverse=True)
        return out[:10]

    if m_v:
        venta = parse_block(m_v.group(1))
    if m_a:
        arr = parse_block(m_a.group(1))

    return venta, arr


def infer_tendencia_publicaciones(publicaciones_activas: int, publicaciones_totales: int) -> str:
    if publicaciones_totales <= 0:
        return "estable"
    ratio = publicaciones_activas / publicaciones_totales
    if ratio >= 0.14:
        return "alza"
    if ratio <= 0.07:
        return "caida"
    return "estable"


def infer_indicadores_mercado(data: Dict[str, Any]) -> Dict[str, str]:
    var_venta = float(data.get("variacion_uf_m2_12m") or 0)
    var_arr = float(data.get("variacion_arriendo_12m") or 0)
    activas = int(data.get("publicaciones_activas") or 0)
    totales = int(data.get("publicaciones_totales") or 0)
    min_uf = int(data.get("min_uf") or 0)
    max_uf = int(data.get("max_uf") or 0)

    ratio_inv = (activas / totales) if totales else 0.0
    dispersion = (max_uf - min_uf) / max(min_uf, 1) if min_uf else 0

    if var_venta < -3 and ratio_inv > 0.10:
        tendencia = "desaceleracion"
    elif var_venta > 3 and ratio_inv < 0.09:
        tendencia = "expansion"
    else:
        tendencia = "estable"

    if ratio_inv < 0.07 and var_venta >= -1:
        liquidez = "alta"
    elif ratio_inv < 0.12:
        liquidez = "media"
    else:
        liquidez = "baja"

    if (var_venta < -4 and ratio_inv > 0.11) or dispersion > 4.0:
        presion = "alta"
    elif var_venta < -1 or ratio_inv > 0.09:
        presion = "media"
    else:
        presion = "baja"

    if activas > 900:
        competencia = "alto"
    elif activas > 300:
        competencia = "medio"
    else:
        competencia = "bajo"

    # arriendo contribuye como ajuste fino
    if var_arr < -5 and presion == "media":
        presion = "alta"

    return {
        "liquidez": liquidez,
        "presion_baja_precio": presion,
        "nivel_competencia": competencia,
        "tendencia_mercado": tendencia,
    }


def build_insights(indicadores: Dict[str, str], data: Dict[str, Any]) -> List[str]:
    insights: List[str] = []
    if indicadores.get("nivel_competencia") == "alto":
        insights.append("mercado_con_alta_competencia")
    if indicadores.get("tendencia_mercado") == "desaceleracion":
        insights.append("desaceleracion_precios")
    if indicadores.get("presion_baja_precio") == "alta":
        insights.append("alta_presion_negociacion")
    if indicadores.get("liquidez") == "baja":
        insights.append("mercado_liquidez_baja")
    if indicadores.get("tendencia_mercado") == "estable":
        insights.append("mercado_estable")
    activas = int(data.get("publicaciones_activas") or 0)
    totales = int(data.get("publicaciones_totales") or 0)
    if totales > 0 and (activas / totales) > 0.12:
        insights.append("sobreoferta_publicaciones")
    if not insights:
        insights.append("mercado_monitoreo_normal")
    return insights


def score_presion_comercial(indicadores: Dict[str, str], data: Dict[str, Any]) -> float:
    score = 0.0
    var_venta = float(data.get("variacion_uf_m2_12m") or 0)
    brecha = float(data.get("brecha_publicacion_vs_cierre_pct") or 0)
    activas = int(data.get("publicaciones_activas") or 0)
    totales = int(data.get("publicaciones_totales") or 0)
    min_uf = float(data.get("min_uf") or 0)
    max_uf = float(data.get("max_uf") or 0)
    ratio = (activas / totales) if totales else 0.0
    dispersion = ((max_uf - min_uf) / max(min_uf, 1.0)) if min_uf else 0.0

    if indicadores.get("liquidez") == "baja":
        score += 20
    elif indicadores.get("liquidez") == "media":
        score += 10

    if indicadores.get("nivel_competencia") == "alto":
        score += 18
    elif indicadores.get("nivel_competencia") == "medio":
        score += 10

    if indicadores.get("presion_baja_precio") == "alta":
        score += 22
    elif indicadores.get("presion_baja_precio") == "media":
        score += 12

    if var_venta < -5:
        score += 18
    elif var_venta < -2:
        score += 10
    elif var_venta > 3:
        score -= 8

    if brecha < -10:
        score += 16
    elif brecha < -5:
        score += 10

    if ratio > 0.12:
        score += 10
    elif ratio > 0.09:
        score += 6

    if dispersion > 4.0:
        score += 8
    elif dispersion > 2.5:
        score += 4

    return round(max(0.0, min(100.0, score)), 2)


def build_resumen_comercial_llm(
    comuna: str,
    tipo_propiedad: str,
    indicadores: Dict[str, str],
    venta: Dict[str, Any],
    arriendo: Dict[str, Any],
) -> str:
    txt = (
        f"El mercado de {tipo_propiedad.lower()} en {comuna} muestra una tendencia {indicadores.get('tendencia_mercado','estable')}, "
        f"con competencia {indicadores.get('nivel_competencia','media')} y liquidez {indicadores.get('liquidez','media')}. "
        f"La UF/m2 de publicacion se ubica en {venta.get('uf_m2_publicacion_actual',0):.2f} y la UF/m2 de cierre efectivo en "
        f"{venta.get('uf_m2_venta_efectiva_actual',0):.2f}, con brecha de {indicadores.get('brecha_publicacion_vs_cierre_pct',0):.2f}%. "
        f"Se observan {venta.get('publicaciones_activas',0)} publicaciones activas en venta y {arriendo.get('publicaciones_arriendo_activas',0)} en arriendo. "
        f"La recomendacion general es {'ajustar expectativas de precio y reforzar diferenciacion comercial' if indicadores.get('presion_baja_precio') == 'alta' else 'mantener seguimiento comercial activo y monitoreo de conversion'}."
    )
    return normalize_ws(txt)[:800]


def build_contexto_mercado(comuna: str, tipo: str, mercado_venta: Dict[str, Any], mercado_arr: Dict[str, Any], indicadores: Dict[str, str]) -> str:
    txt = (
        f"{tipo} en {comuna} con tendencia {indicadores.get('tendencia_mercado','estable')}. "
        f"UF/m2 publicacion actual {mercado_venta.get('uf_m2_publicacion_actual',0):.2f}, "
        f"ventas efectivas {mercado_venta.get('uf_m2_venta_efectiva_actual',0):.2f}, "
        f"variacion 12m {mercado_venta.get('variacion_uf_m2_12m',0):.2f}%. "
        f"Inventario venta activo {mercado_venta.get('publicaciones_activas',0)} de {mercado_venta.get('publicaciones_totales',0)}. "
        f"Arriendo UF/m2 {mercado_arr.get('uf_m2_arriendo_actual',0):.3f} con variacion {mercado_arr.get('variacion_arriendo_12m',0):.2f}%. "
        f"Mercado con liquidez {indicadores.get('liquidez','media')}, presion competitiva {indicadores.get('presion_baja_precio','media')} "
        f"y nivel de competencia {indicadores.get('nivel_competencia','medio')}."
    )
    return normalize_ws(txt)[:1500]


# -------------------------
# Main pipeline
# -------------------------
def ensure_indexes(col: Collection) -> None:
    col.create_index([("comuna", ASCENDING), ("tipo_propiedad", ASCENDING)], unique=True, name="uq_comuna_tipo")
    col.create_index([("source.codigo_informe", ASCENDING)], name="ix_codigo_informe")
    col.create_index([("pdf_control.hash_sha256", ASCENDING)], name="ix_pdf_hash")


def run_pipeline(
    pdf_dir: Path,
    force_reprocess: bool = False,
    only_files: Optional[List[str]] = None,
    limit: Optional[int] = None,
) -> None:
    db = get_db()
    col = db["mercado_comunal"]
    ensure_indexes(col)

    cartera_col = db["universo_cartera"]
    allowed_pairs = set()
    for doc in cartera_col.find({"oficina": "PROCASA SUCRE", "disponible": True}, {"comuna": 1, "tipo": 1}):
        c_raw = doc.get("comuna") or ""
        t_raw = doc.get("tipo") or ""
        if c_raw and t_raw:
            c_norm, t_norm, _ = normalize_comuna_tipo(c_raw, t_raw)
            allowed_pairs.add((c_norm, t_norm))

    logger.info("Cargados %d pares (comuna, tipo) únicos desde universo_cartera", len(allowed_pairs))

    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if only_files:
        wanted = {x.strip().lower() for x in only_files if x.strip()}
        pdfs = [p for p in pdfs if p.name.lower() in wanted]
    if limit and limit > 0:
        pdfs = pdfs[:limit]
    if not pdfs:
        logger.warning("No hay PDFs en %s", pdf_dir)
        return

    for pdf in tqdm(pdfs, desc="Mercado comunal", unit="pdf", dynamic_ncols=True):
        filename = pdf.name
        logger.info("Procesando: %s", filename)

        comuna_from_name, tipo_raw, normalized_slug = parse_comuna_tipo_from_filename(filename)
        if not tipo_raw:
            logger.warning("Tipo no detectado en %s. Se omite.", filename)
            continue
        tipo_propiedad = tipo_raw.replace("_", " ").title()
        file_hash = sha256_file(pdf)

        existing = col.find_one(
            {"pdf_control.filename": filename},
            {"_id": 1, "pdf_control.hash_sha256": 1},
        )
        prev_hash = ((existing or {}).get("pdf_control") or {}).get("hash_sha256")
        if (not force_reprocess) and prev_hash == file_hash:
            logger.info("Sin cambios por hash, omitido: %s", filename)
            continue

        try:
            pages_norm, pages_layout = extract_pdf_pages_text(pdf)
            pages = pages_norm
            all_text = "\n\n".join(pages)

            meta = extract_metadata(pages)
            comuna_pdf = (meta.get("comuna_pdf") or "").strip()
            comuna_final = comuna_pdf or comuna_from_name

            # Si tipo PDF contradice nombre, priorizamos PDF y luego normalizamos.
            tipo_propiedad = normalize_tipo_propiedad(tipo_raw=tipo_raw, tipo_pdf=meta.get("tipo_pdf", ""))
            comuna_final, tipo_propiedad, match_key = normalize_comuna_tipo(comuna_final, tipo_propiedad)

            if allowed_pairs and (comuna_final, tipo_propiedad) not in allowed_pairs:
                logger.info("Omitido %s: (%s, %s) no en universo_cartera actual", filename, comuna_final, tipo_propiedad)
                continue


            page2 = pages[1] if len(pages) > 1 else all_text
            basics = extract_page2_market_basics(page2)

            # Encontrar páginas con matching flexible (OCR/encoding imperfecto)
            page_venta_efectiva_idx = next(
                (
                    i
                    for i, p in enumerate(pages)
                    if "publicaciones venta ventas efectivas" in p.lower()
                    or ("evoluci" in p.lower() and "uf/m2" in p.lower() and "venta" in p.lower() and "efectivas" in p.lower())
                ),
                -1,
            )
            page_arr_ufm2_idx = next(
                (
                    i
                    for i, p in enumerate(pages)
                    if "publicaciones en arriendo" in p.lower()
                    and "uf/m2" in p.lower()
                    and "evoluci" in p.lower()
                ),
                -1,
            )
            page_limits_idx = next(
                (
                    i
                    for i, p in enumerate(pages)
                    if "l" in p.lower() and "publicaciones en venta" in p.lower() and "dispers" in p.lower()
                ),
                -1,
            )
            page_limits_layout = pages_layout[page_limits_idx] if page_limits_idx >= 0 else "\n\n".join(pages_layout)
            page_top = all_text

            layout_texts = []
            if page_venta_efectiva_idx >= 0:
                layout_texts.append(pages_layout[page_venta_efectiva_idx])
            if page_arr_ufm2_idx >= 0 and page_arr_ufm2_idx != page_venta_efectiva_idx:
                layout_texts.append(pages_layout[page_arr_ufm2_idx])
            
            layout_text_combined = "\n".join(layout_texts) if layout_texts else "\n\n".join(pages_layout)

            ufm2 = extract_ufm2_from_page(layout_text_combined)
            ranges = extract_ranges_from_page(page_limits_layout)
            top_venta, top_arr = extract_top_corredores(page_top)

            tendencia_publicaciones = infer_tendencia_publicaciones(
                basics.get("publicaciones_activas", 0), basics.get("publicaciones_totales", 0)
            )

            venta_obj = {
                "uf_m2_publicacion_actual": round(ufm2.get("uf_m2_publicacion_actual", 0.0), 2),
                "uf_m2_venta_efectiva_actual": round(ufm2.get("uf_m2_venta_efectiva_actual", 0.0), 2),
                "variacion_uf_m2_12m": round(ufm2.get("variacion_uf_m2_12m", 0.0), 2),
                "publicaciones_activas": int(basics.get("publicaciones_activas", 0)),
                "publicaciones_totales": int(basics.get("publicaciones_totales", 0)),
                "tendencia_publicaciones": tendencia_publicaciones,
            }

            arriendo_obj = {
                "uf_m2_arriendo_actual": round(ufm2.get("uf_m2_arriendo_actual", 0.0), 3),
                "variacion_arriendo_12m": round(ufm2.get("variacion_arriendo_12m", 0.0), 2),
                "publicaciones_arriendo_activas": int(basics.get("publicaciones_arriendo_activas", 0)),
                "publicaciones_arriendo_totales": int(basics.get("publicaciones_arriendo_totales", 0)),
            }

            indicadores_in = {
                "variacion_uf_m2_12m": venta_obj["variacion_uf_m2_12m"],
                "variacion_arriendo_12m": arriendo_obj["variacion_arriendo_12m"],
                "publicaciones_activas": venta_obj["publicaciones_activas"],
                "publicaciones_totales": venta_obj["publicaciones_totales"],
                "min_uf": int(ranges.get("min_uf", 0)),
                "max_uf": int(ranges.get("max_uf", 0)),
            }
            indicadores = infer_indicadores_mercado(indicadores_in)
            brecha_publicacion_vs_cierre_pct = 0.0
            pub = float(venta_obj["uf_m2_publicacion_actual"] or 0)
            eff = float(venta_obj["uf_m2_venta_efectiva_actual"] or 0)
            if pub > 0:
                brecha_publicacion_vs_cierre_pct = round(((eff - pub) / pub) * 100.0, 2)
            indicadores["brecha_publicacion_vs_cierre_pct"] = brecha_publicacion_vs_cierre_pct
            indicadores["cap_rate"] = float(basics.get("cap_rate", 0.0))
            indicadores["payback_anios"] = int(basics.get("payback_anios", 0))

            score_presion = score_presion_comercial(
                indicadores=indicadores,
                data={
                    **indicadores_in,
                    "brecha_publicacion_vs_cierre_pct": brecha_publicacion_vs_cierre_pct,
                },
            )
            indicadores["score_presion_comercial"] = score_presion
            indicadores["insights_comerciales"] = build_insights(
                indicadores=indicadores,
                data={
                    "publicaciones_activas": venta_obj["publicaciones_activas"],
                    "publicaciones_totales": venta_obj["publicaciones_totales"],
                },
            )

            contexto = build_contexto_mercado(
                comuna=comuna_final,
                tipo=tipo_propiedad,
                mercado_venta=venta_obj,
                mercado_arr=arriendo_obj,
                indicadores=indicadores,
            )
            resumen_comercial = build_resumen_comercial_llm(
                comuna=comuna_final,
                tipo_propiedad=tipo_propiedad,
                indicadores=indicadores,
                venta=venta_obj,
                arriendo=arriendo_obj,
            )

            # segmentacion_superficie: mantenemos placeholder estable si no existe fuente textual confiable
            seg_construccion = "N/D"
            seg_terreno = "N/D"

            doc = {
                "comuna": comuna_final,
                "tipo_propiedad": tipo_propiedad,
                "match_key": match_key,
                "source": {
                    "filename": filename,
                    "codigo_informe": meta.get("codigo_informe", ""),
                    "fecha_reporte": meta.get("fecha_reporte", ""),
                },
                "mercado_venta": venta_obj,
                "mercado_arriendo": arriendo_obj,
                "rangos_precio_venta": {
                    "min_uf": int(ranges.get("min_uf", 0)),
                    "max_uf": int(ranges.get("max_uf", 0)),
                },
                "indicadores_mercado": indicadores,
                "segmentacion_superficie": {
                    "tramo_mas_activo_construccion": seg_construccion,
                    "tramo_mas_activo_terreno": seg_terreno,
                },
                "top_corredores_venta": top_venta,
                "top_corredores_arriendo": top_arr,
                "texto_contexto_mercado": contexto,
                "resumen_comercial_llm": resumen_comercial,
                "pdf_control": {
                    "filename": filename,
                    "hash_sha256": file_hash,
                    "last_processed_at": datetime.now(timezone.utc).isoformat(),
                },
                "pipeline_meta": {
                    "source": "mercado_comunal_pipeline",
                    "version": "1.0.0",
                    "normalized_slug": normalized_slug,
                },
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            logger.info("comuna=%s tipo=%s", comuna_final, tipo_propiedad)
            logger.info("codigo_informe=%s", doc["source"]["codigo_informe"])
            if venta_obj["uf_m2_publicacion_actual"] == 0:
                logger.warning("uf_m2_publicacion_actual no encontrada en %s", filename)
            if arriendo_obj["uf_m2_arriendo_actual"] == 0:
                logger.warning("uf_m2_arriendo_actual no encontrada en %s", filename)

            col.update_one(
                {"comuna": comuna_final, "tipo_propiedad": tipo_propiedad},
                {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc).isoformat()}},
                upsert=True,
            )
            logger.info("Documento actualizado Mongo")

        except Exception as exc:
            logger.exception("Fallo procesando %s: %s", filename, exc)
            continue


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Pipeline mercado comunal")
    ap.add_argument(
        "--pdf-dir",
        default=r"C:\Users\pgall\Desktop\Analisis Comercial 2",
        help="Directorio de PDFs de mercado comunal",
    )
    ap.add_argument("--force-reprocess", action="store_true", help="Reprocesa aunque hash no cambie")
    ap.add_argument("--only-files", default="", help="Lista separada por coma de filenames PDF a procesar")
    ap.add_argument("--limit", type=int, default=0, help="Procesa solo los primeros N archivos")
    ap.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    
    # Silenciar logs ruidosos de pdfminer
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    
    only_files = [x.strip() for x in (args.only_files or "").split(",") if x.strip()]
    run_pipeline(
        Path(args.pdf_dir),
        force_reprocess=args.force_reprocess,
        only_files=only_files or None,
        limit=args.limit or None,
    )


if __name__ == "__main__":
    main()
