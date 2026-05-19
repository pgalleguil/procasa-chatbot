#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Pipeline productivo para integrar tasaciones PDF con universo_cartera,
calcular sobreprecio comercial y preparar contexto + embeddings.

Uso ejemplo:
python scripts/tasaciones_pipeline.py --pdf-dir "C:\\Users\\pgall\\Desktop\\Tasaciones" --embedding-provider none
python scripts/tasaciones_pipeline.py --pdf-dir "C:\\Users\\pgall\\Desktop\\Tasaciones" --embedding-provider fastembed --embedding-mode embedded
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pymongo import ASCENDING, DeleteOne, UpdateOne
from pymongo.collection import Collection
from pymongo.errors import BulkWriteError
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.storage import get_db


# =========================
# Logging
# =========================
logger = logging.getLogger("tasaciones_pipeline")


# =========================
# Parse helpers
# =========================
MONEY_RE = re.compile(r"[-+]?\d[\d\.,\s]*")
ROL_RE = re.compile(r"\b\d{1,5}-\d{1,5}\b")
DATE_RE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")


@dataclass
class EmbeddingConfig:
    provider: str
    mode: str
    model: str


def normalize_whitespace(text: str) -> str:
    text = text.replace("\x00", " ")
    text = text.replace("\ufb01", "fi")
    text = text.replace("\ufb02", "fl")
    text = re.sub(r"[\u200b\u200c\u200d]", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def normalize_for_regex(text: str) -> str:
    text = normalize_whitespace(text)
    return re.sub(r"\s+", " ", text)


def parse_number(value: Optional[str]) -> float:
    if not value:
        return 0.0
    m = MONEY_RE.search(value)
    if not m:
        return 0.0
    raw = m.group(0).strip().replace(" ", "")
    raw = re.sub(r"[^\d,\.\-\+]", "", raw)
    if not raw:
        return 0.0

    # Normalizacion de miles/decimales tolerante para formatos CL.
    if "." in raw and "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    elif raw.count(".") == 1 and "," not in raw:
        # Caso tipico CL de miles: 8.821 -> 8821 ; decimal real: 27.8 -> 27.8
        left, right = raw.split(".", 1)
        if len(right) == 3 and left.replace("+", "").replace("-", "").isdigit():
            raw = left + right
    elif raw.count(",") > 1:
        raw = raw.replace(",", "")
    elif "," in raw and "." not in raw:
        left, right = raw.split(",", 1)
        if len(right) == 3 and left.isdigit():
            raw = left + right
        else:
            raw = left + "." + right

    try:
        return float(raw)
    except ValueError:
        return 0.0


def parse_int(value: Optional[str]) -> int:
    return int(round(parse_number(value)))


def extract_first(patterns: Iterable[str], text: str, flags: int = re.IGNORECASE) -> str:
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m:
            return (m.group(1) or "").strip()
    return ""


def extract_block(start_label: str, end_labels: List[str], raw_text: str) -> str:
    joined_end = "|".join(re.escape(lbl) for lbl in end_labels)
    pattern = rf"{re.escape(start_label)}\s*[:\-]?\s*(.+?)(?=(?:{joined_end})\s*[:\-]?|$)"
    m = re.search(pattern, raw_text, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return normalize_whitespace(m.group(1))


def extract_section_by_number(raw_text: str, section_number: int) -> str:
    # Captura desde "N.-" hasta "(N+1).-", tomando la ultima ocurrencia para evitar el indice.
    pattern = rf"{section_number}\s*\.\s*-\s*(.+?)(?=\b{section_number + 1}\s*\.\s*-|$)"
    matches = list(re.finditer(pattern, raw_text, flags=re.IGNORECASE | re.DOTALL))
    if not matches:
        return ""
    return normalize_whitespace(matches[-1].group(1))


def clean_observaciones(text: str) -> str:
    if not text:
        return ""
    t = normalize_whitespace(text)
    t = re.sub(r"valor\s+uf\s*:\s*\$[\d\.,]+\s*/\s*c[oó]digo\s*:\s*[A-Z0-9\-]+\s+\S+@\S+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"tasaciones?\s+o[\w\s]*ciales?\.", "tasaciones oficiales.", t, flags=re.IGNORECASE)
    m_focus = re.search(r"(Los informes de tasaci[oó]n online.+?tasaciones oficiales\.)", t, flags=re.IGNORECASE | re.DOTALL)
    if m_focus:
        return normalize_whitespace(m_focus.group(1))[:800]
    # Si se contaminó con páginas completas, recortamos.
    return normalize_whitespace(t)[:1200]


def parse_comparables_section(section_text: str) -> List[Dict[str, Any]]:
    if not section_text:
        return []
    rows: List[Dict[str, Any]] = []
    lines = [normalize_whitespace(x) for x in section_text.split("\n") if normalize_whitespace(x)]
    for ln in lines:
        if not re.search(r"\b\d{1,3}(?:[\.,]\d+)?\b", ln):
            continue
        m_uf = re.search(r"(\d[\d\.,]*)\s*$", ln)
        m_ufm2 = re.search(r"(\d[\d\.,]*)\s+(?:\d[\d\.,]*)\s*$", ln)
        m_fecha = re.search(r"\b\d{2}-\d{2}-\d{4}\b", ln)
        if not m_uf:
            continue
        rows.append(
            {
                "raw_line": ln,
                "fecha": m_fecha.group(0) if m_fecha else "",
                "uf_m2": parse_number(m_ufm2.group(1)) if m_ufm2 else 0.0,
                "valor_uf": parse_number(m_uf.group(1)),
            }
        )
    return rows


def parse_puntos_interes(raw_text: str) -> Dict[str, List[str]]:
    out = {"transporte": [], "educacion": [], "salud": [], "comercio": [], "areas_verdes": []}
    blocks = list(
        re.finditer(
            r"\d+\s*\.\s*-\s*Puntos\s+de\s+inter[eé]s\s*(.+?)\s*\d+\s*\.\s*-\s*Observaciones",
            raw_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    if not blocks:
        return out

    # Evita capturar el indice: elegimos el bloque con mayor evidencia de contenido real.
    best_block = ""
    best_score = -1
    for m in blocks:
        candidate = normalize_whitespace(m.group(1))
        score = 0
        lc = candidate.lower()
        for token in ("transporte", "educaci", "salud", "comercio", "areas verdes", "áreas verdes"):
            if token in lc:
                score += 2
        score += len(re.findall(r"\d+\.\s+", candidate))
        if score > best_score:
            best_score = score
            best_block = candidate

    if not best_block:
        return out

    block = best_block
    block = block.replace("Áreas Verdes", "Areas Verdes")
    block = block.replace("áreas verdes", "areas verdes")

    patterns = [
        ("transporte", r"Transporte\s*(.+?)(?=Educaci[oó]n|Salud|Comercio|Areas Verdes|areas verdes|$)"),
        ("educacion", r"Educaci[oó]n\s*(.+?)(?=Salud|Comercio|Areas Verdes|areas verdes|$)"),
        ("salud", r"Salud\s*(.+?)(?=Comercio|Areas Verdes|areas verdes|$)"),
        ("comercio", r"Comercio\s*(.+?)(?=Areas Verdes|areas verdes|$)"),
        ("areas_verdes", r"(?:Areas Verdes|areas verdes)\s*(.+?)$"),
    ]

    def extract_bullets(section: str) -> List[str]:
        items: List[str] = []
        lines = [normalize_whitespace(x) for x in section.split("\n") if normalize_whitespace(x)]
        curr = ""
        for ln in lines:
            if re.match(r"^\d+\.\s*", ln):
                if curr:
                    items.append(curr.strip())
                curr = re.sub(r"^\d+\.\s*", "", ln).strip()
            else:
                if curr:
                    curr = f"{curr} {ln}".strip()
        if curr:
            items.append(curr.strip())

        cleaned: List[str] = []
        for it in items:
            # Conserva solo bullets con distancia en metros.
            m_dist = re.search(r"\((\d+\s*mts)\)\s*$", it, flags=re.IGNORECASE)
            if not m_dist:
                continue
            dist = m_dist.group(1)
            name = re.sub(r"\(\d+\s*mts\)\s*$", "", it, flags=re.IGNORECASE).strip(" -")
            cleaned.append(f"{normalize_whitespace(name)} ({dist})")
        return cleaned

    for key, pat in patterns:
        mm = re.search(pat, block, flags=re.IGNORECASE | re.DOTALL)
        if not mm:
            continue
        section = mm.group(1)
        if key == "transporte":
            # Caso OCR frecuente: "Transporte Educación" sin items de transporte.
            if re.match(r"^\s*educaci[oó]n\b", section, flags=re.IGNORECASE):
                out[key] = []
                continue
        out[key] = extract_bullets(section)

    return out


def norm_line(s: str) -> str:
    return normalize_whitespace(s).lower()


def extract_value_after_label(lines: List[str], labels: List[str], max_lookahead: int = 3) -> str:
    labels_norm = [norm_line(x) for x in labels]
    for i, line in enumerate(lines):
        n = norm_line(line)
        for lbl in labels_norm:
            if n.startswith(lbl + ":"):
                val = line.split(":", 1)[1].strip()
                if val:
                    return normalize_whitespace(val)
                for j in range(1, max_lookahead + 1):
                    if i + j < len(lines):
                        candidate = normalize_whitespace(lines[i + j])
                        if candidate and not candidate.lower().startswith("valor uf:"):
                            return candidate
            if n == lbl:
                for j in range(1, max_lookahead + 1):
                    if i + j < len(lines):
                        candidate = normalize_whitespace(lines[i + j])
                        if candidate and not candidate.lower().startswith("valor uf:"):
                            return candidate
    return ""


def extract_number_between_labels(lines: List[str], first_label: str, second_label: str, max_gap: int = 6) -> float:
    l1 = norm_line(first_label)
    l2 = norm_line(second_label)
    for i, line in enumerate(lines):
        if norm_line(line) == l1:
            for j in range(i + 1, min(i + max_gap + 1, len(lines))):
                if norm_line(lines[j]) == l2:
                    for k in range(j - 1, i, -1):
                        v = parse_number(lines[k])
                        if v > 0:
                            return v
    return 0.0


# =========================
# PDF extraction
# =========================
def extract_text_from_pdf(pdf_path: Path) -> str:
    # Prefer pypdf (estable en estos informes), fallback pdfplumber, then pymupdf.
    text_parts: List[str] = []

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        for page in reader.pages:
            text_parts.append(page.extract_text() or "")
        text = "\n".join(text_parts).strip()
        if text:
            return text
    except Exception as exc:
        logger.debug("pypdf fallo en %s: %s", pdf_path.name, exc)

    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
        text = "\n".join(text_parts).strip()
        if text:
            return text
    except Exception as exc:
        logger.debug("pdfplumber fallo en %s: %s", pdf_path.name, exc)

    try:
        import fitz  # type: ignore

        with fitz.open(str(pdf_path)) as doc:
            for page in doc:
                text_parts.append(page.get_text("text") or "")
        return "\n".join(text_parts).strip()
    except Exception as exc:
        logger.warning("No se pudo extraer texto de %s: %s", pdf_path.name, exc)
        return ""


def parse_tasacion_fields(codigo: str, raw_text: str) -> Dict[str, Any]:
    text = normalize_for_regex(raw_text)
    lines = [normalize_whitespace(x) for x in raw_text.splitlines() if normalize_whitespace(x)]

    m_valor = re.search(
        r"estimaci[oó]n\s+de\s+valor\s+comercial\s*([\d\.,]+)\s*uf\s*\$([\d\.,]+)",
        text,
        re.IGNORECASE,
    )
    if m_valor:
        valor_uf = parse_int(m_valor.group(1))
        valor_clp = parse_int(m_valor.group(2))
    else:
        valor_uf = parse_int(
            extract_first(
                [
                    r"estimaci[oó]n\s+de\s+valor\s+comercial\s*([\d\.,\s]+)\s*uf",
                    r"estimaci[oó]n\s+de\s+valor\s+comercial\s*[:\-]?\s*([\d\.,\s]+)",
                ],
                text,
            )
        )
        valor_clp = parse_int(
            extract_first([r"estimaci[oó]n\s+de\s+valor\s+comercial\s*[\d\.,\s]+uf\s*\$([\d\.,\s]+)"], text)
        )

    m_arr = re.search(
        r"estimaci[oó]n\s+de\s+arriendo\s*([\d\.,]+)\s*uf\s*\$([\d\.,]+)",
        text,
        re.IGNORECASE,
    )
    if m_arr:
        arriendo_uf = round(parse_number(m_arr.group(1)), 2)
        arriendo_clp = parse_int(m_arr.group(2))
    else:
        arriendo_uf = round(
            parse_number(
            extract_first(
                [
                    r"estimaci[oó]n\s+de\s+arriendo\s*([\d\.,\s]+)\s*uf",
                    r"estimaci[oó]n\s+de\s+arriendo\s*[:\-]?\s*([\d\.,\s]+)",
                ],
                text,
            )),
            2,
        )
        arriendo_clp = parse_int(extract_first([r"estimaci[oó]n\s+de\s+arriendo\s*[\d\.,\s]+uf\s*\$([\d\.,\s]+)"], text))

    precio_min = parse_int(extract_first([r"(\d[\d\.\,]*)\s*uf\s*precio\s*m[ií]nimo"], text))
    precio_est = parse_int(extract_first([r"(\d[\d\.\,]*)\s*uf\s*estimaci[oó]n\s*de?\s*valor\s*comercial"], text))
    precio_max = parse_int(extract_first([r"(\d[\d\.\,]*)\s*uf\s*precio\s*m[aá]ximo"], text))
    if not precio_min:
        precio_min = parse_int(extract_number_between_labels(lines, "3.-Valor mínimo-máximo", "Precio mínimo"))
    if not precio_est:
        precio_est = parse_int(extract_number_between_labels(lines, "3.-Valor mínimo-máximo", "Estimación de valor comercial"))
    if not precio_max:
        precio_max = parse_int(extract_number_between_labels(lines, "3.-Valor mínimo-máximo", "Precio máximo"))

    direccion_sii = extract_first(
        [
            r"direcci[oó]n\s+sii\s+(.+?)\s+comuna\s",
            r"direcci[oó]n\s+propiedad\s*:\s*(.+?)\s+emitido\s+por",
        ],
        text,
    )
    comuna = extract_first([r"comuna\s+([a-zA-Záéíóúñ\s]+?)\s+rol\s"], text)

    rol = extract_first([r"rol\s+(\d{1,5}\s*-\s*\d{1,5})"], text).replace(" ", "")
    if not rol:
        rol_match = ROL_RE.search(text)
        rol = rol_match.group(0) if rol_match else ""

    ano_construccion = parse_int(extract_first([r"a[nñ]o\s+de\s+construcci[oó]n\s+(\d{4})"], text))
    total_construccion_m2 = parse_int(extract_first([r"total\s+construcci[oó]n\s+(\d[\d\.,]*)\s*m2"], text))
    superficie_terraza_m2 = parse_int(
        extract_first(
            [
                r"super\w*\s+terraza\s+(\d[\d\.,]*)\s*m2",
                r"super\w*\s+terreno\s+\*?\s*(\d[\d\.,]*)\s*m2",
                r"super\s*\w*\s*terraza\s+(\d[\d\.,]*)\s*m2",
                r"super\s*\w*\s*terreno\s+\*?\s*(\d[\d\.,]*)\s*m2",
            ],
            text,
        )
    )

    indicadores_section = ""
    for sec in (13, 14, 15, 16):
        candidate = extract_section_by_number(raw_text, sec)
        if "indicadores inmobiliarios" in candidate.lower():
            indicadores_section = candidate
            break
    ind_text = normalize_for_regex(indicadores_section or raw_text)

    plusvalia = parse_number(
        extract_first(
            [
                r"plusval[ií]a.*?([-+]?\d+[\.,]\d+)\s*%",
                r"plusval[ií]a.*?([-+]?\d+)\s*%",
            ],
            ind_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    rentabilidad = parse_number(
        extract_first(
            [
                r"rentabilidad.*?([-+]?\d+[\.,]\d+)\s*%",
                r"rentabilidad.*?([-+]?\d+)\s*%",
            ],
            ind_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    retorno_anios = parse_number(
        extract_first(
            [
                r"retorno.*?(\d+[\.,]?\d*)\s*a[nñ]os",
            ],
            ind_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )

    fecha_informe = extract_first([r"fecha\s+informe\s*[:\-]?\s*([\d\-/]+)"], text)
    if not fecha_informe:
        date_match = DATE_RE.search(text)
        fecha_informe = date_match.group(1) if date_match else ""

    codigo_informe = extract_first(
        [r"c[oó]digo\s+informe\s*[:\-]?\s*([a-zA-Z0-9\-_]+)", r"c[oó]digo\s*:\s*([A-Z0-9\-]+)"],
        text,
    )

    # Secciones reales por numeracion (evita capturar solo el indice)
    descripcion_sector = ""
    for sec in (10, 11, 12):
        s = extract_section_by_number(raw_text, sec)
        if "descripción del sector" in s.lower() or "descripcion del sector" in s.lower():
            descripcion_sector = s
            break
    comentarios_mercado = ""
    for sec in (18, 19, 20, 21):
        s = extract_section_by_number(raw_text, sec)
        if "comentarios del mercado" in s.lower():
            comentarios_mercado = s
            break
    if not descripcion_sector:
        descripcion_sector = extract_block("descripcion sector", ["comentarios mercado", "indicadores", "observaciones"], raw_text)
    if not comentarios_mercado:
        comentarios_mercado = extract_block("comentarios mercado", ["indicadores", "observaciones", "puntos de interes"], raw_text)
    observaciones_raw = extract_block("observaciones", ["fecha informe", "codigo informe"], raw_text)
    observaciones_clean = clean_observaciones(observaciones_raw)

    puntos_interes = parse_puntos_interes(raw_text)
    comparables_venta = parse_comparables_section(extract_section_by_number(raw_text, 18))
    comparables_oferta_venta = parse_comparables_section(extract_section_by_number(raw_text, 19))
    comparables_oferta_arriendo = parse_comparables_section(extract_section_by_number(raw_text, 20))
    comparables_mismo_conjunto = parse_comparables_section(extract_section_by_number(raw_text, 22))

    return {
        "codigo_propiedad": codigo,
        "tasacion_online": {
            "direccion_sii": direccion_sii,
            "comuna": comuna.strip(),
            "rol": rol,
            "ano_construccion": ano_construccion,
            "total_construccion_m2": total_construccion_m2,
            "superficie_terraza_m2": superficie_terraza_m2,
            "valor_comercial": {"uf": valor_uf, "clp": valor_clp},
            "arriendo_estimado": {"uf": arriendo_uf, "clp": arriendo_clp},
            "valor_minimo_maximo": {
                "precio_minimo_uf": precio_min,
                "estimacion_valor_comercial_uf": precio_est,
                "precio_maximo_uf": precio_max,
            },
            "descripcion_sector": descripcion_sector,
            "comentarios_mercado": comentarios_mercado,
            "indicadores_inmobiliarios": {
                "plusvalia": plusvalia,
                "rentabilidad": rentabilidad,
                "retorno_anios": retorno_anios,
            },
            "puntos_interes": puntos_interes,
            "observaciones_raw": observaciones_raw,
            "observaciones_clean": observaciones_clean,
            "fecha_informe": fecha_informe,
            "codigo_informe": codigo_informe,
            "comparables_venta": comparables_venta,
            "comparables_oferta_venta": comparables_oferta_venta,
            "comparables_oferta_arriendo": comparables_oferta_arriendo,
            "comparables_mismo_conjunto": comparables_mismo_conjunto,
        },
    }


# =========================
# Commercial analysis
# =========================
def months_between(iso_date: Optional[str], now_utc: datetime) -> Optional[float]:
    if not iso_date:
        return None
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta_days = (now_utc - dt.astimezone(timezone.utc)).days
        return max(delta_days / 30.0, 0.0)
    except Exception:
        return None


def safe_ratio(a: float, b: float) -> float:
    if not b:
        return 0.0
    return a / b


def build_argumento_comercial(
    codigo: str,
    precio_publicado_uf: float,
    valor_tasacion_uf: float,
    diff_pct: float,
    meses_publicacion: Optional[float],
    comentarios_mercado: str,
    plusvalia: float,
    rentabilidad: float,
    retorno_anios: float,
    estrategia_tipo: str,
) -> str:
    meses_txt = f"{meses_publicacion:.1f}" if meses_publicacion is not None else "N/D"
    tendencia = "negativa" if plusvalia < 0 else "estable/positiva"
    base = (
        f"La propiedad codigo {codigo} registra un precio publicado de {precio_publicado_uf:,.0f} UF "
        f"versus una tasacion comercial estimada en {valor_tasacion_uf:,.0f} UF, con brecha de {diff_pct:.1f}%. "
        f"El activo acumula {meses_txt} meses en cartera. "
        f"Los indicadores del sector muestran plusvalia {plusvalia:.2f}% ({tendencia}), "
        f"rentabilidad {rentabilidad:.2f}% y retorno estimado en {retorno_anios:.1f} anios. "
        f"{comentarios_mercado[:280]} "
    ).strip()
    if estrategia_tipo == "baja_precio_urgente":
        return base + " Se recomienda evaluar ajuste de precio para recuperar competitividad y acelerar cierre."
    if estrategia_tipo == "destacar_oportunidad":
        return base + " El precio se ubica bajo referencia de tasacion; se recomienda reforzar marketing y captacion de leads."
    if estrategia_tipo == "revisar_dato_publicacion":
        return base + " Se detecta posible inconsistencia en precio publicado; validar dato en cartera antes de activar recomendaciones."
    return base + " El precio se observa en rango competitivo; se recomienda mantener estrategia actual con seguimiento."


def build_contexto_comercial(
    tasa_doc: Dict[str, Any],
    cartera_doc: Dict[str, Any],
    argumento: str,
    estrategia_tipo: str,
) -> str:
    tas = tasa_doc.get("tasacion_online", {})
    indicadores = tas.get("indicadores_inmobiliarios", {})
    precio_pub = cartera_doc.get("precio_uf", 0)
    precio_tas = (tas.get("valor_comercial") or {}).get("uf", 0)
    resumen_mercado = str(tas.get("comentarios_mercado", ""))[:280]
    parts = [
        f"codigo: {tasa_doc.get('codigo_propiedad','')}",
        f"tipo: {cartera_doc.get('tipo', '')}",
        f"comuna: {cartera_doc.get('comuna', tas.get('comuna', ''))}",
        f"precio_publicado_uf: {precio_pub}",
        f"tasacion_uf: {precio_tas}",
        f"estrategia: {estrategia_tipo}",
        f"plusvalia: {indicadores.get('plusvalia', 0)}",
        f"rentabilidad: {indicadores.get('rentabilidad', 0)}",
        f"retorno_anios: {indicadores.get('retorno_anios', 0)}",
        f"mercado: {resumen_mercado}",
        argumento,
        f"orientacion: {cartera_doc.get('orientacion', '')}",
        f"caracteristicas: {cartera_doc.get('caracteristicas_relevantes', '')}",
    ]
    txt = normalize_whitespace(" | ".join(p for p in parts if p))
    return txt[:1500]


def compute_analisis_comercial(tasa_doc: Dict[str, Any], cartera_doc: Dict[str, Any]) -> Tuple[Dict[str, Any], str, str]:
    now_utc = datetime.now(timezone.utc)

    tas = tasa_doc.get("tasacion_online", {})
    ind = tas.get("indicadores_inmobiliarios", {})

    precio_publicado_uf = float(cartera_doc.get("precio_uf") or 0)
    valor_tasacion_uf = float((tas.get("valor_comercial") or {}).get("uf") or 0)

    diff_uf = precio_publicado_uf - valor_tasacion_uf
    diff_pct = safe_ratio(diff_uf, valor_tasacion_uf) * 100.0
    ratio = safe_ratio(precio_publicado_uf, valor_tasacion_uf)
    margen_sobreprecio = max(diff_pct, 0.0)

    plusvalia = float(ind.get("plusvalia") or 0)
    rentabilidad = float(ind.get("rentabilidad") or 0)
    retorno_anios = float(ind.get("retorno_anios") or 0)

    fecha_incorporacion = cartera_doc.get("fecha_incorporacion")
    ultima_actualizacion = cartera_doc.get("ultima_actualizacion")
    historial_cambios = cartera_doc.get("historial_cambios") or []

    meses_publicacion = months_between(str(fecha_incorporacion), now_utc)
    meses_sin_movimiento = months_between(str(ultima_actualizacion), now_utc)

    score = 0.0
    estado = "alineada"
    prioridad = "baja"
    estrategia_tipo = "mantener_precio"
    calidad_datos = {"precio_publicado_valido": True, "flags": []}

    if ratio > 1.15:
        estado = "sobrevalorada"
        prioridad = "alta"
        estrategia_tipo = "baja_precio_urgente"
        score += 45
    elif ratio > 1.05:
        estado = "sobreprecio_leve"
        prioridad = "media"
        estrategia_tipo = "baja_precio_moderada"
        score += 25
    elif ratio < 0.9:
        estado = "bajo_mercado"
        prioridad = "baja"
        estrategia_tipo = "destacar_oportunidad"

    if precio_publicado_uf <= 0 or precio_publicado_uf < 300:
        calidad_datos["precio_publicado_valido"] = False
        calidad_datos["flags"].append("precio_publicado_uf_anomalo")
        estado = "revision_dato"
        prioridad = "media"
        estrategia_tipo = "revisar_dato_publicacion"

    if plusvalia < 0:
        score += 12
    if rentabilidad < 3.5:
        score += 10
    if retorno_anios > 25:
        score += 8
    if meses_publicacion and meses_publicacion >= 6:
        score += min(20, (meses_publicacion - 6) * 2)
    if meses_sin_movimiento and meses_sin_movimiento >= 2:
        score += min(12, (meses_sin_movimiento - 2) * 2)
    if isinstance(historial_cambios, list) and len(historial_cambios) <= 1:
        score += 6

    score = float(max(0.0, min(100.0, score)))

    analisis = {
        "estado_precio": estado,
        "prioridad_ajuste": prioridad,
        "estrategia_comercial": {"tipo": estrategia_tipo},
        "calidad_datos": calidad_datos,
        "diferencia_uf": round(diff_uf, 2),
        "diferencia_porcentual": round(diff_pct, 2),
        "ratio_publicacion_vs_tasacion": round(ratio, 4),
        "margen_sobreprecio": round(margen_sobreprecio, 2),
        "score_oportunidad_baja_precio": round(score, 2),
        "tiempo_en_cartera_meses": round(meses_publicacion, 2) if meses_publicacion is not None else None,
        "meses_sin_actualizacion": round(meses_sin_movimiento, 2) if meses_sin_movimiento is not None else None,
        "factores": {
            "plusvalia": plusvalia,
            "rentabilidad": rentabilidad,
            "retorno_anios": retorno_anios,
            "historial_cambios_count": len(historial_cambios) if isinstance(historial_cambios, list) else 0,
        },
        "updated_at": now_utc.isoformat(),
    }

    argumento = build_argumento_comercial(
        codigo=str(tasa_doc.get("codigo_propiedad", "")),
        precio_publicado_uf=precio_publicado_uf,
        valor_tasacion_uf=valor_tasacion_uf,
        diff_pct=diff_pct,
        meses_publicacion=meses_publicacion,
        comentarios_mercado=str(tas.get("comentarios_mercado", "")),
        plusvalia=plusvalia,
        rentabilidad=rentabilidad,
        retorno_anios=retorno_anios,
        estrategia_tipo=estrategia_tipo,
    )
    contexto = build_contexto_comercial(tasa_doc, cartera_doc, argumento, estrategia_tipo)
    return analisis, argumento, contexto


# =========================
# Embeddings
# =========================
def generate_embedding(text: str, config: EmbeddingConfig) -> Optional[List[float]]:
    if config.provider == "none" or not text.strip():
        return None

    if config.provider == "fastembed":
        from fastembed import TextEmbedding

        model = TextEmbedding(model_name=config.model)
        vec = next(model.embed([text])).tolist()
        return vec

    if config.provider == "openai":
        from openai import OpenAI

        client = OpenAI()
        resp = client.embeddings.create(model=config.model, input=text)
        return list(resp.data[0].embedding)

    raise ValueError(f"Proveedor de embedding no soportado: {config.provider}")


# =========================
# DB pipeline
# =========================
def ensure_indexes(tasaciones: Collection, embeddings_col: Collection) -> None:
    deduplicate_tasaciones_by_codigo(tasaciones)
    tasaciones.create_index([("codigo_propiedad", ASCENDING)], unique=True, name="uq_codigo_propiedad")
    tasaciones.create_index([("pdf_control.hash_sha256", ASCENDING)], name="ix_pdf_hash")
    tasaciones.create_index([("analisis_comercial.score_oportunidad_baja_precio", ASCENDING)], name="ix_score_baja")
    embeddings_col.create_index([("codigo_propiedad", ASCENDING)], unique=True, name="uq_embeddings_codigo")


def deduplicate_tasaciones_by_codigo(tasaciones: Collection) -> None:
    pipeline = [
        {"$match": {"codigo_propiedad": {"$exists": True, "$ne": ""}}},
        {"$group": {"_id": "$codigo_propiedad", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
    ]
    dup_groups = list(tasaciones.aggregate(pipeline))
    if not dup_groups:
        logger.info("No se detectaron duplicados en tasaciones por codigo_propiedad.")
        return

    logger.warning("Se detectaron %s codigos duplicados. Iniciando deduplicacion...", len(dup_groups))
    delete_ops: List[DeleteOne] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for grp in tqdm(dup_groups, desc="Deduplicando tasaciones", unit="codigo"):
        codigo = grp["_id"]
        docs = list(
            tasaciones.find(
                {"codigo_propiedad": codigo},
                {
                    "_id": 1,
                    "pdf_control.last_processed_at": 1,
                    "updated_at": 1,
                    "created_at": 1,
                    "pipeline_meta.version": 1,
                },
            )
        )
        if len(docs) <= 1:
            continue

        def score_doc(d: Dict[str, Any]) -> Tuple[int, str]:
            last_proc = (((d.get("pdf_control") or {}).get("last_processed_at")) or "")
            updated = str(d.get("updated_at") or "")
            created = str(d.get("created_at") or "")
            version = str(((d.get("pipeline_meta") or {}).get("version")) or "")
            return (1 if version else 0, last_proc or updated or created, str(d.get("_id")))

        docs_sorted = sorted(docs, key=score_doc, reverse=True)
        keep_doc = docs_sorted[0]
        to_delete = docs_sorted[1:]
        for d in to_delete:
            delete_ops.append(DeleteOne({"_id": d["_id"]}))

        tasaciones.update_one(
            {"_id": keep_doc["_id"]},
            {
                "$set": {
                    "pipeline_meta.deduplicated_at": now_iso,
                    "pipeline_meta.deduplicated_count": len(to_delete),
                }
            },
        )

    if delete_ops:
        tasaciones.bulk_write(delete_ops, ordered=False)
        logger.info("Deduplicacion completada. Documentos eliminados: %s", len(delete_ops))


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest_pdfs(
    pdf_dir: Path,
    tasaciones: Collection,
    batch_size: int,
    only_codes: Optional[List[str]] = None,
    force_reprocess: bool = False,
    reprocess_zero_uf: bool = True,
) -> Tuple[int, int]:
    pdf_files = sorted(pdf_dir.glob("*.pdf"))
    only_set = set(only_codes or [])
    if only_set:
        pdf_files = [p for p in pdf_files if p.stem.strip() in only_set]
    if not pdf_files:
        logger.warning("No se encontraron PDFs en %s", pdf_dir)
        return 0, 0

    ops: List[UpdateOne] = []
    processed = 0
    skipped = 0

    for pdf_path in tqdm(pdf_files, desc="Ingesta PDFs", unit="pdf", dynamic_ncols=True):
        codigo = pdf_path.stem.strip()
        if not codigo:
            continue

        try:
            file_hash = file_sha256(pdf_path)
            existing = tasaciones.find_one(
                {"codigo_propiedad": codigo},
                {"_id": 1, "pdf_control.hash_sha256": 1, "tasacion_online.valor_comercial.uf": 1},
            )
            prev_hash = ((existing or {}).get("pdf_control") or {}).get("hash_sha256")
            existing_uf = float((((existing or {}).get("tasacion_online") or {}).get("valor_comercial") or {}).get("uf") or 0)
            should_reprocess_zero = reprocess_zero_uf and existing and existing_uf <= 0
            if (not force_reprocess) and prev_hash and prev_hash == file_hash and (not should_reprocess_zero):
                skipped += 1
                continue

            raw_text = extract_text_from_pdf(pdf_path)
            if not raw_text.strip():
                logger.warning("PDF sin texto util: %s", pdf_path.name)
                continue

            parsed_doc = parse_tasacion_fields(codigo, raw_text)
            parsed_doc["pdf_control"] = {
                "filename": pdf_path.name,
                "hash_sha256": file_hash,
                "last_processed_at": datetime.now(timezone.utc).isoformat(),
            }
            parsed_doc["pipeline_meta"] = {
                "source": "tasaciones_pipeline",
                "version": "1.0.0",
            }

            ops.append(
                UpdateOne(
                    {"codigo_propiedad": codigo},
                    {
                        "$set": parsed_doc,
                        "$setOnInsert": {"created_at": datetime.now(timezone.utc).isoformat()},
                    },
                    upsert=True,
                )
            )
            processed += 1

            if len(ops) >= batch_size:
                tasaciones.bulk_write(ops, ordered=False)
                ops.clear()
        except Exception as exc:
            logger.exception("Error procesando %s: %s", pdf_path, exc)

    if ops:
        tasaciones.bulk_write(ops, ordered=False)

    return processed, skipped


def reset_derived_fields(tasaciones: Collection, codigos: Optional[List[str]] = None) -> int:
    filt: Dict[str, Any] = {}
    if codigos:
        filt = {"codigo_propiedad": {"$in": codigos}}
    res = tasaciones.update_many(
        filt,
        {
            "$unset": {
                "analisis_comercial": "",
                "argumento_baja_precio": "",
                "texto_contexto_comercial": "",
                "vector_contexto_comercial": "",
                "embedding_meta": "",
                "codigo_cartera": "",
            }
        },
    )
    return res.modified_count


def update_commercial_analysis(
    tasaciones: Collection,
    cartera: Collection,
    embeddings_col: Collection,
    embedding_cfg: EmbeddingConfig,
    embedding_batch_size: int,
    only_codes: Optional[List[str]] = None,
) -> Tuple[int, int]:
    query = {"tasacion_online.valor_comercial.uf": {"$gt": 0}}
    if only_codes:
        query["codigo_propiedad"] = {"$in": only_codes}
    total_docs = tasaciones.count_documents(query)
    cursor = tasaciones.find(query, {"codigo_propiedad": 1, "tasacion_online": 1})

    ops_tasa: List[UpdateOne] = []
    ops_embed: List[UpdateOne] = []
    updated = 0
    missing_in_cartera = 0

    for tasa_doc in tqdm(cursor, total=total_docs, desc="Cruce comercial", unit="prop", dynamic_ncols=True):
        codigo = str(tasa_doc.get("codigo_propiedad", "")).strip()
        if not codigo:
            continue

        cartera_doc = cartera.find_one(
            {"codigo": codigo},
            {
                "codigo": 1,
                "precio_uf": 1,
                "ultima_actualizacion": 1,
                "historial_cambios": 1,
                "fecha_incorporacion": 1,
                "resumen_ejecutivo": 1,
                "analisis_ia": 1,
                "descripcion_clean": 1,
                "tipo": 1,
                "comuna": 1,
                "orientacion": 1,
                "caracteristicas_relevantes": 1,
            },
        )

        if not cartera_doc:
            missing_in_cartera += 1
            continue

        analisis, argumento, contexto = compute_analisis_comercial(tasa_doc, cartera_doc)

        set_fields: Dict[str, Any] = {
            "codigo_cartera": cartera_doc.get("codigo"),
            "analisis_comercial": analisis,
            "argumento_baja_precio": argumento,
            "texto_contexto_comercial": contexto,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        vector = generate_embedding(contexto, embedding_cfg)
        if embedding_cfg.mode == "embedded" and vector is not None:
            set_fields["vector_contexto_comercial"] = vector
            set_fields["embedding_meta"] = {
                "provider": embedding_cfg.provider,
                "model": embedding_cfg.model,
                "dim": len(vector),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

        ops_tasa.append(UpdateOne({"codigo_propiedad": codigo}, {"$set": set_fields}))

        if embedding_cfg.mode == "separate" and vector is not None:
            ops_embed.append(
                UpdateOne(
                    {"codigo_propiedad": codigo},
                    {
                        "$set": {
                            "codigo_propiedad": codigo,
                            "embedding": vector,
                            "provider": embedding_cfg.provider,
                            "model": embedding_cfg.model,
                            "dim": len(vector),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                        "$setOnInsert": {"created_at": datetime.now(timezone.utc).isoformat()},
                    },
                    upsert=True,
                )
            )

        if len(ops_tasa) >= embedding_batch_size:
            tasaciones.bulk_write(ops_tasa, ordered=False)
            ops_tasa.clear()
            updated += embedding_batch_size

        if len(ops_embed) >= embedding_batch_size:
            embeddings_col.bulk_write(ops_embed, ordered=False)
            ops_embed.clear()

    if ops_tasa:
        updated += len(ops_tasa)
        tasaciones.bulk_write(ops_tasa, ordered=False)
    if ops_embed:
        embeddings_col.bulk_write(ops_embed, ordered=False)

    return updated, missing_in_cartera


def run_pipeline(args: argparse.Namespace) -> None:
    db = get_db()
    tasaciones = db["tasaciones"]
    cartera = db["universo_cartera"]
    embeddings_col = db["tasaciones_embeddings"]

    ensure_indexes(tasaciones, embeddings_col)

    only_codes = [x.strip() for x in (args.only_codes or "").split(",") if x.strip()]
    if args.reset_derived_fields:
        reset_count = reset_derived_fields(tasaciones, codigos=only_codes or None)
        logger.info("Campos derivados limpiados en %s documentos", reset_count)

    embedding_cfg = EmbeddingConfig(
        provider=args.embedding_provider,
        mode=args.embedding_mode,
        model=args.embedding_model,
    )

    processed, skipped = ingest_pdfs(
        Path(args.pdf_dir),
        tasaciones,
        batch_size=args.batch_size,
        only_codes=only_codes or None,
        force_reprocess=args.force_reprocess,
        reprocess_zero_uf=not args.no_reprocess_zero_uf,
    )
    logger.info("Ingesta completada. Procesados=%s | Omitidos por hash=%s", processed, skipped)

    updated, missing = update_commercial_analysis(
        tasaciones=tasaciones,
        cartera=cartera,
        embeddings_col=embeddings_col,
        embedding_cfg=embedding_cfg,
        embedding_batch_size=args.batch_size,
        only_codes=only_codes or None,
    )

    logger.info("Analisis comercial actualizado=%s | sin match en cartera=%s", updated, missing)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pipeline masivo de tasaciones + analisis comercial")
    parser.add_argument(
        "--pdf-dir",
        default=r"C:\Users\pgall\Desktop\Tasaciones",
        help="Directorio de PDFs de tasaciones",
    )
    parser.add_argument("--batch-size", type=int, default=200, help="Tamano de lote para bulk_write")
    parser.add_argument(
        "--only-codes",
        default="",
        help="Lista separada por coma para procesar solo ciertos codigos (ej: 6687,16348)",
    )
    parser.add_argument(
        "--reset-derived-fields",
        action="store_true",
        help="Limpia campos derivados (analisis/argumento/contexto/vector) antes de reprocesar",
    )
    parser.add_argument(
        "--embedding-provider",
        choices=["none", "fastembed", "openai"],
        default="none",
        help="Proveedor para generar embeddings",
    )
    parser.add_argument(
        "--embedding-mode",
        choices=["embedded", "separate"],
        default="separate",
        help="Guardar vector en tasaciones o en tasaciones_embeddings",
    )
    parser.add_argument(
        "--embedding-model",
        default="sentence-transformers/all-MiniLM-L6-v2",
        help="Modelo embeddings (fastembed/openai)",
    )
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument(
        "--force-reprocess",
        action="store_true",
        help="Reprocesa PDFs aunque el hash sea igual al ultimo procesado",
    )
    parser.add_argument(
        "--no-reprocess-zero-uf",
        action="store_true",
        help="Desactiva reproceso automatico cuando existe tasacion guardada con UF=0 y hash sin cambios",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    try:
        run_pipeline(args)
    except BulkWriteError as bwe:
        logger.exception("Bulk write error: %s", bwe.details)
        raise
    except Exception:
        logger.exception("Fallo no controlado en pipeline")
        raise


if __name__ == "__main__":
    main()
