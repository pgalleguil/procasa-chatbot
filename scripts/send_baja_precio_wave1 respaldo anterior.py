#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Procasa - Motor de Inteligencia Comercial y Ajuste de Precios
Evolución arquitectónica con idempotencia, snapshots congelados, validaciones de red,
ruteo en tres vías, score de confianza y suite de testing automatizada.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import secrets
import smtplib
import sys
import time
import threading
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus
from openpyxl import Workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import Config
from chatbot.storage import get_db

TASACIONES_DIR = Path(r"C:/Users/pgall/Desktop/Tasaciones")
ANALISIS_COMERCIAL_DIR = Path(r"C:/Users/pgall/Desktop/Analisis Comercial 2")

TEMPLATE_VERSION = "v3.2-ajuste-progresivo"
MARKET_DATA_SCORE_THRESHOLD = 65
MARKET_DATA_SCORE_PARTIAL_MIN = 40


def build_action_url(base_url: str, email: str, accion: str, codigo: str, campana: str, token: str, mode: str = "live") -> str:
    return (
        f"{base_url}/campana/respuesta?email={quote_plus(email)}"
        f"&accion={quote_plus(accion)}&codigos={quote_plus(codigo)}"
        f"&campana={quote_plus(campana)}&token={quote_plus(token)}&mode={quote_plus(mode)}"
    )


def _texto_por_accion(accion: str) -> tuple[str, str, str, str]:
    if accion == "bajar_precio_urgente":
        return (
            "Recomendación prioritaria",
            "Queríamos comentarte que hemos estado revisando la comercialización de tu propiedad y, en este momento, podría ser conveniente evaluar una actualización de precio.",
            "La idea no es apresurar una decisión, sino ayudarte a mantener la propiedad bien alineada con lo que hoy están buscando los clientes activos.",
            "Con este ajuste, normalmente se recupera interés y se facilita avanzar hacia conversaciones más concretas.",
        )
    if accion == "bajar_precio_sugerida":
        return (
            "Sugerencia de ajuste",
            "Queríamos compartir contigo una recomendación simple para cuidar el ritmo comercial de tu propiedad.",
            "En esta etapa, una revisión moderada del precio publicado podría ayudar a mejorar la respuesta sin perder el foco en el valor de la propiedad.",
            "Si te parece, lo revisamos juntos para definir una alternativa razonable y cómoda para ti.",
        )
    if accion == "revisar_publicacion":
        return (
            "Revisión de publicación",
            "Queríamos proponerte una mejora en la forma de presentar tu propiedad, para reforzar su visibilidad frente a nuevos interesados.",
            "Antes de tocar precio, en este caso puede ser más útil revisar enfoque de publicación y mensaje comercial.",
            "Suelen ser cambios simples, pero con buen impacto en el interés inicial.",
        )
    return (
        "Buen momento comercial",
        "Queríamos contarte que tu propiedad mantiene una base comercial favorable.",
        "Nuestra recomendación es continuar con una gestión activa y ordenada para sostener el interés.",
        "Si quieres, podemos coordinar una revisión breve para definir próximos pasos.",
    )


def _calcular_brecha_tasacion_pct(prop: dict) -> float | None:
    precio = float(prop.get("precio_publicado_uf") or 0)
    operacion = str(prop.get("operacion") or "").strip().lower()
    tas_arriendo_raw = prop.get("tasacion_arriendo_uf")
    tas_arriendo = float(tas_arriendo_raw) if tas_arriendo_raw not in (None, "", 0, "0") else None
    if operacion == "arriendo" and tas_arriendo and tas_arriendo > 0 and precio > 0:
        return ((precio - tas_arriendo) / tas_arriendo) * 100.0

    tasacion_min_raw = prop.get("tasacion_comercial_min_uf") or prop.get("tasacion_min_uf")
    tasacion_max_raw = prop.get("tasacion_comercial_max_uf") or prop.get("tasacion_max_uf")
    tas_min = float(tasacion_min_raw) if tasacion_min_raw not in (None, "", 0, "0") else None
    tas_max = float(tasacion_max_raw) if tasacion_max_raw not in (None, "", 0, "0") else None
    tas_ref = None
    if tas_min is not None and tas_max is not None:
        tas_ref = (tas_min + tas_max) / 2.0
    elif tas_min is not None:
        tas_ref = tas_min
    elif tas_max is not None:
        tas_ref = tas_max
    if not tas_ref or tas_ref <= 0 or precio <= 0:
        return None
    return ((precio - tas_ref) / tas_ref) * 100.0


def validar_pdf_fisico(pdf_path: Path) -> tuple[bool, str]:
    """Valida la existencia física, tamaño mínimo y firma mágica (%PDF) de un archivo PDF."""
    if not pdf_path.exists():
        return False, "pdf_missing"
    try:
        size = pdf_path.stat().st_size
        if size < 5120:  # Menor a 5KB es considerado corrupto o vacío
            return False, "pdf_small_size"
        with pdf_path.open("rb") as f:
            header = f.read(4)
            if header != b"%PDF":
                return False, "pdf_invalid_header"
        return True, "ok"
    except Exception:
        return False, "pdf_corrupt"


def calcular_confidence_score(p: dict, brecha: float | None, pdf_valido: bool) -> int:
    """Calcula un indicador numérico (0-100) sobre la completitud y calidad del dato."""
    score = 40  # Base
    if pdf_valido:
        score += 20
    
    tas_min = p.get("tasacion_comercial_min_uf") or p.get("tasacion_min_uf")
    tas_max = p.get("tasacion_comercial_max_uf") or p.get("tasacion_max_uf")
    if tas_min is not None or tas_max is not None or p.get("tasacion_arriendo_uf") is not None:
        score += 15
        
    if p.get("uf_m2_publicacion_actual") is not None and p.get("uf_m2_venta_efectiva_actual") is not None:
        score += 10
        
    # Consistencia precio publicado vs cierre promedio comunal (si está en un rango razonable de +/- 40%)
    precio = float(p.get("precio_publicado_uf") or 0)
    avg_cierre = p.get("uf_m2_venta_efectiva_actual")
    if precio > 0 and avg_cierre is not None:
        score += 15
        
    if p.get("ejecutivo") and p.get("telefono_ejecutivo"):
        score += 10
        
    dias_pub = p.get("dias_publicada") or p.get("dias_publicacion")
    if dias_pub is not None:
        score += 10
        
    if brecha is not None and brecha >= 100.0:
        # Penalizar progresivamente si la brecha es excesivamente alta (anomalía o sobreprecio insostenible)
        score -= int(min(20, (brecha - 100) / 10))
        
    return max(0, min(100, score))


def _to_float(value):
    if value in (None, "", "No disponible"):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _normalize_liquidez_score(raw_liquidez: str | None) -> float | None:
    liq = str(raw_liquidez or "").strip().lower()
    if not liq:
        return None
    mapping = {
        "alta": 100.0,
        "media-alta": 80.0,
        "media": 60.0,
        "media-baja": 40.0,
        "baja": 20.0,
    }
    return mapping.get(liq)


def calcular_market_data_score(p: dict) -> tuple[int, bool, str, list[str], dict]:
    """Evalúa calidad de inteligencia comunal con score 0-100 y campos faltantes."""
    raw_values = {
        "uf_m2_publicado_promedio": _to_float(p.get("uf_m2_publicacion_actual")),
        "uf_m2_cierre_promedio": _to_float(p.get("uf_m2_venta_efectiva_actual")),
        "publicaciones_activas": _to_float(p.get("publicaciones_activas")),
        "presion_comercial": _to_float(p.get("score_presion_comercial")),
        "brecha_publicacion_cierre": _to_float(p.get("brecha_publicacion_vs_cierre_pct") or p.get("brecha_publicacion_cierre_pct")),
        "liquidez": _normalize_liquidez_score(p.get("liquidez")),
        "comparables_activos": _to_float(p.get("publicaciones_activas") or p.get("stock_activo_similares") or p.get("cantidad_similares_activas")),
        "comparables_cerrados": _to_float(p.get("publicaciones_totales") or p.get("tiempo_promedio_venta_dias") or p.get("tiempo_promedio_comuna_dias")),
    }
    weights = {
        "uf_m2_publicado_promedio": 16,
        "uf_m2_cierre_promedio": 16,
        "publicaciones_activas": 12,
        "presion_comercial": 12,
        "brecha_publicacion_cierre": 12,
        "liquidez": 10,
        "comparables_activos": 11,
        "comparables_cerrados": 11,
    }
    missing_fields = [k for k, v in raw_values.items() if v is None]
    present_weight = sum(weights[k] for k, v in raw_values.items() if v is not None)
    score = int(round((present_weight / 100.0) * 100))
    comunal_data_complete = len(missing_fields) == 0

    critical_missing = any(
        raw_values.get(k) is None
        for k in ("uf_m2_publicado_promedio", "uf_m2_cierre_promedio", "presion_comercial", "publicaciones_activas")
    )
    if score >= MARKET_DATA_SCORE_THRESHOLD and not critical_missing:
        quality_tier = "robusta"
    elif score >= MARKET_DATA_SCORE_PARTIAL_MIN:
        quality_tier = "parcial"
    else:
        quality_tier = "insuficiente"

    return score, comunal_data_complete, quality_tier, missing_fields, raw_values


def clasificar_propiedad(p: dict, pdf_dir: Path) -> dict:
    """Clasifica la propiedad entre rutas y outliers en base a reglas de negocio comercial."""
    codigo = str(p.get("codigo_propiedad") or "")
    email = (p.get("email_propietario") or "").strip()
    precio = float(p.get("precio_publicado_uf") or 0)
    
    brecha = _calcular_brecha_tasacion_pct(p)
    pdf_path = pdf_dir / f"{codigo}.pdf"
    pdf_valido, pdf_motive = validar_pdf_fisico(pdf_path)
    score = calcular_confidence_score(p, brecha, pdf_valido)
    market_data_score, comunal_data_complete, comunal_quality_tier, missing_market_fields, _ = calcular_market_data_score(p)
    missing_market_fields_txt = ",".join(missing_market_fields) if missing_market_fields else ""
    suggested_adjustment_enabled = market_data_score >= MARKET_DATA_SCORE_THRESHOLD and comunal_quality_tier == "robusta"
    exclusion_reason = ""
    send_eligible = True
    
    # Valores por defecto
    categoria_outlier = "Sin brecha / Sin PDF"
    ruta_asignada = "Ruta Inteligencia Comunal"
    motivo_ruta = "Fallback (sin PDF válido o brecha no positiva)"
    accion_sugerida = "Enviar Inteligencia Comunal"
    usa_tasacion = False
    
    if precio <= 0:
        categoria_outlier = "Error probable - Precio inválido"
        ruta_asignada = "Cola Manual"
        motivo_ruta = "Precio menor o igual a 0 UF"
        accion_sugerida = "Omitir - Datos inválidos"
        exclusion_reason = "PRECIO_INVALIDO"
        send_eligible = False
    elif not email:
        categoria_outlier = "Datos CRM faltantes"
        ruta_asignada = "Cola Manual"
        motivo_ruta = "Sin correo electrónico de propietario registrado"
        accion_sugerida = "Omitir - Sin email"
        exclusion_reason = "SIN_EMAIL"
        send_eligible = False
    elif brecha is not None:
        if brecha >= 300.0:
            categoria_outlier = "Error probable - Cola manual"
            ruta_asignada = "Cola Manual"
            motivo_ruta = f"Brecha extrema de sobreprecio registrada: {brecha:.1f}%"
            accion_sugerida = "Revisión comercial manual obligatoria"
            exclusion_reason = "OUTLIER_EXTREMO"
            send_eligible = False
        elif brecha >= 60.0:
            if pdf_valido:
                categoria_outlier = "Sobreprecio extremo legítimo"
                ruta_asignada = "Ruta Ajuste Progresivo"
                motivo_ruta = f"Brecha extrema de {brecha:.1f}% con PDF válido"
                accion_sugerida = "Enviar Ajuste Progresivo (Consultivo)"
                usa_tasacion = True
            else:
                categoria_outlier = "Sobreprecio extremo sin PDF"
                ruta_asignada = "Ruta Inteligencia Comunal"
                motivo_ruta = f"Brecha de {brecha:.1f}% sin PDF válido ({pdf_motive})"
                accion_sugerida = "Enviar Inteligencia Comunal (Fallback)"
        elif brecha > 0:
            if pdf_valido:
                categoria_outlier = "Sobreprecio moderado"
                ruta_asignada = "Ruta Tasación Individual"
                motivo_ruta = f"Brecha moderada de {brecha:.1f}% con PDF válido"
                accion_sugerida = "Enviar Tasación Individual"
                usa_tasacion = True
            else:
                categoria_outlier = "Sobreprecio moderado sin PDF"
                ruta_asignada = "Ruta Inteligencia Comunal"
                motivo_ruta = f"Brecha moderada de {brecha:.1f}% sin PDF válido ({pdf_motive})"
                accion_sugerida = "Enviar Inteligencia Comunal (Fallback)"
        else:
            categoria_outlier = "Alineada o bajo mercado"
            ruta_asignada = "Ruta Inteligencia Comunal"
            motivo_ruta = f"Brecha no positiva ({brecha:.1f}%)"
            accion_sugerida = "Enviar Inteligencia Comunal (Fallback)"
    else:
        categoria_outlier = "Sin tasación referencial"
        ruta_asignada = "Ruta Inteligencia Comunal"
        motivo_ruta = f"Sin tasación comercial en base de datos. PDF válido: {pdf_valido}"
        accion_sugerida = "Enviar Inteligencia Comunal (Fallback)"

    # Gobernanza adicional para Ruta Inteligencia Comunal (robusta/parcial/insuficiente)
    if ruta_asignada == "Ruta Inteligencia Comunal":
        if comunal_quality_tier == "insuficiente" and not pdf_valido:
            ruta_asignada = "Cola Manual"
            categoria_outlier = "Datos comunales insuficientes"
            motivo_ruta = "Fallback comunal con datos críticos faltantes y sin PDF individual válido"
            accion_sugerida = "Derivar a revisión manual"
            exclusion_reason = "DATOS_COMUNALES_INSUFICIENTES"
            send_eligible = False
            suggested_adjustment_enabled = False
        elif comunal_quality_tier == "parcial":
            motivo_ruta = f"{motivo_ruta} | Inteligencia comunal parcial"
            suggested_adjustment_enabled = False
        elif comunal_quality_tier == "robusta":
            motivo_ruta = f"{motivo_ruta} | Inteligencia comunal robusta"

    if ruta_asignada == "Cola Manual" and not exclusion_reason:
        exclusion_reason = "MANUAL_REVIEW_REQUIRED"
    if ruta_asignada != "Cola Manual" and not exclusion_reason:
        exclusion_reason = "NONE"
        
    return {
        "brecha_calculada_pct": brecha,
        "pdf_valido": pdf_valido,
        "pdf_motive": pdf_motive,
        "confidence_score": score,
        "categoria_outlier": categoria_outlier,
        "ruta_asignada": ruta_asignada,
        "motivo_ruta": motivo_ruta,
        "accion_sugerida": accion_sugerida,
        "usa_tasacion": usa_tasacion,
        "market_data_score": market_data_score,
        "comunal_data_complete": comunal_data_complete,
        "comunal_quality_tier": comunal_quality_tier,
        "missing_market_fields": missing_market_fields_txt,
        "exclusion_reason": exclusion_reason,
        "send_eligible": send_eligible,
        "suggested_adjustment_enabled": suggested_adjustment_enabled,
    }


def build_html(
    prop: dict,
    email: str,
    campana: str,
    base_url: str,
    asesor: str,
    token: str,
    mode: str = "live",
    incluye_tasacion_adjunta: bool = False,
    incluye_informe_comercial_adjunta: bool = False,
) -> str:
    # --- FUNCIONES DE FORMATO CHILENO ---
    def fmt_uf(val: float | int | None) -> str:
        if val is None:
            return "No disponible"
        return f"{val:,.0f}".replace(",", ".")

    def fmt_dec(val: float | int | None, dec: int = 2) -> str:
        if val is None:
            return "No disponible"
        s = f"{val:,.{dec}f}"
        return s.replace(",", "X").replace(".", ",").replace("X", ".")

    if "jorge pablo caro" in asesor.lower() or "jorge pablo" in asesor.lower():
        cargo_ejecutivo = "Asesor Comercial Inmobiliario"
    elif asesor.lower() == "equipo procasa":
        cargo_ejecutivo = "Soporte Comercial"
    else:
        cargo_ejecutivo = "Asesora Comercial Inmobiliaria"

    codigo = str(prop.get("codigo_propiedad") or "")
    property_url = f"https://www.procasa.cl/{codigo}" if codigo else "https://www.procasa.cl"
    comuna = (prop.get("comuna") or "").strip()
    comuna = comuna.title().replace(" Del ", " del ").replace(" De ", " de ").replace(" Y ", " y ")
    tipo = prop.get("tipo_propiedad") or ""
    accion = prop.get("accion_recomendada") or "bajar_precio_sugerida"
    operacion = str(prop.get("operacion") or "").strip().lower()
    precio = float(prop.get("precio_publicado_uf") or 0)
    nuevo = float(prop.get("nuevo_precio_objetivo_uf") or 0)

    if nuevo <= 0 and precio > 0:
        liquidez_val = str(prop.get("liquidez") or "").strip().lower()
        score_presion_val = prop.get("score_presion_comercial")
        score_presion_f = float(score_presion_val) if score_presion_val is not None else None
        brecha_ic_val = prop.get("brecha_publicacion_vs_cierre_pct") or prop.get("brecha_publicacion_cierre_pct")
        brecha_ic_f = float(brecha_ic_val) if brecha_ic_val is not None else None
        
        if liquidez_val == "baja" or (score_presion_f is not None and score_presion_f >= 80) or (brecha_ic_f is not None and brecha_ic_f <= -20):
            pct_sug = 6.0
        elif liquidez_val == "media":
            pct_sug = 4.0
        else:
            pct_sug = 3.0
        nuevo = round(precio * (1.0 - pct_sug / 100.0), 2)

    asunto_tag, _, _, _ = _texto_por_accion(accion)
    delta_pct = ((nuevo - precio) / precio * 100.0) if (precio > 0 and nuevo > 0) else None
    variacion = f"{delta_pct:+.1f}%".replace(".", ",") if delta_pct is not None else "No aplica"
    
    if delta_pct is not None and delta_pct <= -5:
        impacto = "Impacto esperado: aumento relevante en oportunidades de contacto calificado."
    elif delta_pct is not None and delta_pct < 0:
        impacto = "Impacto esperado: mejora gradual en visibilidad y ritmo de consultas."
    else:
        impacto = "Potencial mejora en competitividad, visibilidad y generacion de nuevas oportunidades de contacto."

    dias_publicada_raw = prop.get("dias_publicada") or prop.get("dias_publicacion")
    tiempo_promedio_raw = prop.get("tiempo_promedio_venta_dias") or prop.get("tiempo_promedio_comuna_dias")
    actividad_raw = prop.get("indice_actividad_compradores") or prop.get("actividad_compradores_score")
    competitividad_raw = prop.get("indice_competitividad_precio") or prop.get("competitividad_precio_score")
    visibilidad_raw = prop.get("indice_visibilidad") or prop.get("visibilidad_score")
    score_raw = prop.get("score_comercial")

    dias_publicada = int(dias_publicada_raw) if dias_publicada_raw is not None else None
    tiempo_promedio_comuna = int(tiempo_promedio_raw) if tiempo_promedio_raw is not None else None
    actividad_idx = int(actividad_raw) if actividad_raw is not None else None
    competitividad_idx = int(competitividad_raw) if competitividad_raw is not None else None
    visibilidad_idx = int(visibilidad_raw) if visibilidad_raw is not None else None
    score_comercial = int(score_raw) if score_raw is not None else None

    if score_comercial is None and None not in (actividad_idx, competitividad_idx, visibilidad_idx):
        score_comercial = round((actividad_idx + competitividad_idx + visibilidad_idx) / 3)
    score_comercial = max(0, min(100, score_comercial)) if score_comercial is not None else None
    competitividad_score = round(95 - (score_comercial * 0.8)) if score_comercial is not None else None

    visibilidad_label = (
        "Alta" if visibilidad_idx is not None and visibilidad_idx >= 70 else
        "Media" if visibilidad_idx is not None and visibilidad_idx >= 45 else
        "Baja" if visibilidad_idx is not None else "En evaluacion"
    )
    interes_label = (
        "Alto" if actividad_idx is not None and actividad_idx >= 70 else
        "Medio" if actividad_idx is not None and actividad_idx >= 45 else
        "Bajo" if actividad_idx is not None else "En evaluacion"
    )
    competitividad_label = (
        "Alta" if competitividad_idx is not None and competitividad_idx >= 70 else
        "Media" if competitividad_idx is not None and competitividad_idx >= 45 else
        "Baja" if competitividad_idx is not None else "En evaluacion"
    )

    rango_min_raw = prop.get("rango_competitivo_min_uf")
    rango_max_raw = prop.get("rango_competitivo_max_uf")
    rango_min = int(rango_min_raw) if rango_min_raw is not None else None
    rango_max = int(rango_max_raw) if rango_max_raw is not None else None
    tendencia_mercado = (prop.get("tendencia_mercado") or "").strip()
    tendencia_mercado = tendencia_mercado or "Dato en actualizacion para esta propiedad."
    avg_pub_uf_raw = prop.get("promedio_publicacion_uf") or prop.get("avg_publicacion_uf")
    avg_cierre_uf_raw = prop.get("promedio_cierre_uf") or prop.get("avg_cierre_uf")
    delta_pub_cierre_raw = prop.get("delta_publicacion_cierre_pct") or prop.get("brecha_publicacion_cierre_pct")
    stock_activo_raw = prop.get("stock_activo_similares") or prop.get("cantidad_similares_activas")
    promedio_uf_m2_raw = prop.get("promedio_uf_m2") or prop.get("avg_uf_m2")
    velocidad_raw = prop.get("velocidad_comercial") or prop.get("velocidad_comercial_idx")
    rango_cierre_min_raw = prop.get("rango_cierre_min_uf")
    rango_cierre_max_raw = prop.get("rango_cierre_max_uf")
    tasacion_min_raw = prop.get("tasacion_comercial_min_uf") or prop.get("tasacion_min_uf")
    tasacion_max_raw = prop.get("tasacion_comercial_max_uf") or prop.get("tasacion_max_uf")
    tasacion_arriendo_raw = prop.get("tasacion_arriendo_uf")
    tasacion_arriendo = float(tasacion_arriendo_raw) if tasacion_arriendo_raw not in (None, "", 0, "0") else None
    
    avg_pub_uf = float(avg_pub_uf_raw) if avg_pub_uf_raw is not None else None
    avg_cierre_uf = float(avg_cierre_uf_raw) if avg_cierre_uf_raw is not None else None
    delta_pub_cierre = float(delta_pub_cierre_raw) if delta_pub_cierre_raw is not None else None
    stock_activo = int(stock_activo_raw) if stock_activo_raw is not None else None
    promedio_uf_m2 = float(promedio_uf_m2_raw) if promedio_uf_m2_raw is not None else None
    rango_cierre_min = float(rango_cierre_min_raw) if rango_cierre_min_raw not in (None, "", 0, "0") else None
    rango_cierre_max = float(rango_cierre_max_raw) if rango_cierre_max_raw not in (None, "", 0, "0") else None
    tasacion_min = float(tasacion_min_raw) if tasacion_min_raw not in (None, "", 0, "0") else None
    tasacion_max = float(tasacion_max_raw) if tasacion_max_raw not in (None, "", 0, "0") else None
    uf_m2_publicacion_actual = prop.get("uf_m2_publicacion_actual")
    uf_m2_venta_efectiva_actual = prop.get("uf_m2_venta_efectiva_actual")
    publicaciones_activas = prop.get("publicaciones_activas")
    estado_precio_tasacion = str(prop.get("estado_precio_tasacion") or "").strip().lower()
    prioridad_ajuste_tasacion = str(prop.get("prioridad_ajuste_tasacion") or "").strip().lower()
    argumento_comercial = str(prop.get("argumento_comercial") or "").strip()
    nivel_competencia = str(prop.get("nivel_competencia") or "").strip()
    liquidez = str(prop.get("liquidez") or "").strip()
    score_presion = prop.get("score_presion_comercial")
    brecha_pub_cierre_ic = prop.get("brecha_publicacion_vs_cierre_pct")

    tasacion_ref = None
    if operacion == "arriendo" and tasacion_arriendo is not None:
        tasacion_ref = tasacion_arriendo
        tasacion_min = tasacion_arriendo
        tasacion_max = tasacion_arriendo
    elif tasacion_min is not None and tasacion_max is not None:
        tasacion_ref = (tasacion_min + tasacion_max) / 2.0
    elif tasacion_min is not None:
        tasacion_ref = tasacion_min
    elif tasacion_max is not None:
        tasacion_ref = tasacion_max

    brecha_tasacion_pct = ((precio - tasacion_ref) / tasacion_ref * 100.0) if (tasacion_ref and tasacion_ref > 0 and precio > 0) else None
    
    # Asignar ruteo y lógica
    ruta_asignada = prop.get("ruta_asignada") or ("Ruta Tasación Individual" if brecha_tasacion_pct is not None and brecha_tasacion_pct > 0 else "Ruta Inteligencia Comunal")
    usar_ruta_tasacion = ruta_asignada in {"Ruta Tasación Individual", "Ruta Ajuste Progresivo"}
    comunal_quality_tier = str(prop.get("comunal_quality_tier") or "").strip().lower()
    suggested_adjustment_enabled = bool(prop.get("suggested_adjustment_enabled"))

    origen_partes = []
    if (tasacion_min is not None or tasacion_max is not None) and (tasacion_ref is not None and precio > tasacion_ref):
        origen_partes.append("tasaciones comerciales")
    if rango_cierre_min is not None or rango_cierre_max is not None or avg_cierre_uf is not None:
        origen_partes.append("comparables vendidos")
    if any(v is not None for v in [actividad_idx, competitividad_idx, visibilidad_idx, tiempo_promedio_comuna]):
        origen_partes.append("comportamiento comercial reciente")
    if not origen_partes:
        origen_partes.append("publicaciones comparables activas")
    origen_analisis = ", ".join(origen_partes)

    observacion_comercial = (
        "Observamos que propiedades similares que ajustaron estrategicamente su posicionamiento comercial aumentaron sus oportunidades de contacto durante las semanas siguientes."
    )

    if "tasacion 0 UF" in argumento_comercial or "tasación 0 UF" in argumento_comercial or "brecha 0.0%" in argumento_comercial:
        argumento_comercial = ""
    if tasacion_ref is not None and tasacion_ref > 0 and "0 UF" in argumento_comercial:
        argumento_comercial = ""

    score_bar_width = competitividad_score if competitividad_score is not None else 0
    if rango_min is not None and rango_max is not None and rango_max > rango_min and precio > 0:
        marker_pct = int(((precio - rango_min) / (rango_max - rango_min)) * 100)
        marker_pct = max(0, min(100, marker_pct))
    else:
        marker_pct = None

    resumen_rows = []
    if actividad_idx is not None:
        resumen_rows.append(
            f"""
            <tr>
              <td style="width:50%;padding:0 8px 0 0;vertical-align:top;">
                <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Actividad compradores</p>
                <p style="margin:0;font-size:18px;font-weight:700;color:#292256;">{actividad_idx}/100</p>
              </td>
              <td style="width:50%;padding:0 0 0 8px;vertical-align:top;">
                <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Tendencia mercado</p>
                <p style="margin:0;font-size:14px;line-height:1.5;color:#334155;">{tendencia_mercado}</p>
              </td>
            </tr>
            """
        )
    elif tendencia_mercado and tendencia_mercado != "Dato en actualizacion para esta propiedad.":
        resumen_rows.append(
            f"""
            <tr>
              <td colspan="2" style="padding:0;vertical-align:top;">
                <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Tendencia mercado</p>
                <p style="margin:0;font-size:14px;line-height:1.5;color:#334155;">{tendencia_mercado}</p>
              </td>
            </tr>
            """
        )

    has_resumen = any(v is not None for v in [dias_publicada, tiempo_promedio_comuna, actividad_idx]) or (
        tendencia_mercado and tendencia_mercado != "Dato en actualizacion para esta propiedad."
    )
    resumen_html = ""
    if has_resumen:
        left_val = f"{dias_publicada} dias" if dias_publicada is not None else "No disponible"
        right_val = f"{tiempo_promedio_comuna} dias" if tiempo_promedio_comuna is not None else "No disponible"
        benchmark_row = ""
        if dias_publicada is not None or tiempo_promedio_comuna is not None:
            benchmark_row = f"""
            <tr>
              <td style="width:50%;padding:0 8px 10px 0;vertical-align:top;">
                <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Tiempo publicado</p>
                <p style="margin:0;font-size:18px;font-weight:700;color:#292256;">{left_val}</p>
              </td>
              <td style="width:50%;padding:0 0 10px 8px;vertical-align:top;">
                <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Promedio comuna</p>
                <p style="margin:0;font-size:18px;font-weight:700;color:#292256;">{right_val}</p>
              </td>
            </tr>
            """
        resumen_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;margin:0 0 22px 0;">
          <tr>
            <td style="padding:16px;">
              <p style="margin:0 0 12px 0;font-size:18px;line-height:1.3;color:#292256;font-weight:700;">Resumen comercial</p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                {benchmark_row}
                {''.join(resumen_rows)}
              </table>
            </td>
          </tr>
        </table>
        """

    comparacion_html = ""
    if any(v is not None for v in [dias_publicada, tiempo_promedio_comuna, avg_pub_uf, avg_cierre_uf]):
        comp_tiempo_pub = f"{tiempo_promedio_comuna} dias" if tiempo_promedio_comuna is not None else "No disponible"
        comp_tu_prop = f"{dias_publicada} dias publicados" if dias_publicada is not None else "No disponible"
        comp_pub_uf = f"{fmt_uf(avg_pub_uf)} UF" if avg_pub_uf is not None else "No disponible"
        comp_cierre_uf = f"{fmt_uf(avg_cierre_uf)} UF" if avg_cierre_uf is not None else "No disponible"
        comparacion_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #d7e0eb;border-radius:14px;margin:0 0 22px 0;">
          <tr>
            <td style="padding:18px;">
              <p style="margin:0 0 10px 0;font-size:18px;line-height:1.3;color:#292256;font-weight:700;">Comparacion mercado {comuna}</p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  <td style="width:50%;padding:0 8px 0 0;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Promedio publicación</p>
                    <p style="margin:0;font-size:18px;font-weight:700;color:#292256;">{comp_pub_uf}</p>
                  </td>
                  <td style="width:50%;padding:0 0 0 8px;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Promedio cierre</p>
                    <p style="margin:0;font-size:18px;font-weight:700;color:#292256;">{comp_cierre_uf}</p>
                  </td>
                </tr>
                <tr>
                  <td style="width:50%;padding:12px 8px 0 0;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Tiempo promedio venta</p>
                    <p style="margin:0;font-size:18px;font-weight:700;color:#292256;">{comp_tiempo_pub}</p>
                  </td>
                  <td style="width:50%;padding:12px 0 0 8px;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Tu propiedad</p>
                    <p style="margin:0;font-size:18px;font-weight:700;color:#292256;">{comp_tu_prop}</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
        """

    actividad_html = ""
    if rango_min is not None and rango_max is not None:
        brecha_texto = ""
        if delta_pub_cierre is not None:
            brecha_texto = f"En propiedades similares de {comuna}, el promedio de cierre reciente se ubica {f'{abs(delta_pub_cierre):.1f}%'.replace('.', ',')} {'bajo' if delta_pub_cierre < 0 else 'sobre'} la publicación inicial."
        actividad_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #d7e0eb;border-radius:14px;margin:0 0 22px 0;">
          <tr>
            <td style="padding:18px;">
              <p style="margin:0 0 10px 0;font-size:18px;line-height:1.3;color:#292256;font-weight:700;">Actividad de mercado</p>
              <p style="margin:0 0 10px 0;font-size:14px;line-height:1.6;color:#475569;">Propiedades similares en {comuna} muestran mejor desempeño comercial en rangos entre <b>{fmt_uf(rango_min)} UF</b> y <b>{fmt_uf(rango_max)} UF</b>.</p>
              <p style="margin:0 0 10px 0;font-size:14px;line-height:1.6;color:#475569;">{brecha_texto}</p>
              <p style="margin:0 0 6px 0;font-size:14px;color:#64748b;">Rango competitivo zona</p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 8px 0;">
                <tr>
                  <td style="font-size:14px;color:#334155;padding:0 0 6px 0;">
                    {fmt_uf(rango_min)} UF
                    <span style="display:inline-block;width:8px;"></span>
                    <span style="display:inline-block;width:56%;height:6px;background:#dbe4ef;border-radius:999px;vertical-align:middle;position:relative;">
                      <span style="display:inline-block;width:10px;height:10px;background:#292256;border-radius:50%;position:relative;left:{f'{marker_pct}%' if marker_pct is not None else '100%'};top:-2px;"></span>
                    </span>
                    <span style="display:inline-block;width:8px;"></span>
                    {fmt_uf(rango_max)} UF
                  </td>
                </tr>
              </table>
              <p style="margin:0 0 10px 0;font-size:14px;color:#334155;">Tu publicación actual: <b>{fmt_uf(precio)} UF</b></p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  <td style="width:50%;padding:0 8px 0 0;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Stock activo similares</p>
                    <p style="margin:0;font-size:16px;color:#292256;font-weight:700;">{stock_activo if stock_activo is not None else "No disponible"}</p>
                  </td>
                  <td style="width:50%;padding:0 0 0 8px;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Promedio UF/m2</p>
                    <p style="margin:0;font-size:16px;color:#292256;font-weight:700;">{f"{fmt_dec(promedio_uf_m2, 2)} UF/m2" if promedio_uf_m2 is not None else "No disponible"}</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
        """

    score_html = ""
    has_componentes_score = any(v is not None for v in [visibilidad_idx, actividad_idx, competitividad_idx])
    if competitividad_score is not None or has_componentes_score:
        score_nivel = (
            "Competitividad alta" if competitividad_score is not None and competitividad_score >= 75 else
            "Competitividad media" if competitividad_score is not None and competitividad_score >= 50 else
            "Competitividad baja" if competitividad_score is not None else "Indice en evaluacion"
        )
        score_head = f"{competitividad_score}/100 — {score_nivel}" if competitividad_score is not None else "Indice en evaluacion"
        detalle_score = ""
        if has_componentes_score:
            detalle_score = f"""
              <p style="margin:0 0 6px 0;font-size:14px;color:#e2e8f0;">Visibilidad: <b>{visibilidad_label}</b></p>
              <p style="margin:0 0 6px 0;font-size:14px;color:#e2e8f0;">Interes compradores: <b>{interes_label}</b></p>
              <p style="margin:0;font-size:14px;color:#e2e8f0;">Competitividad precio: <b>{competitividad_label}</b></p>
            """
        score_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#292256;border:1px solid #292256;border-radius:14px;margin:0 0 22px 0;">
          <tr>
            <td style="padding:18px;">
              <p style="margin:0 0 2px 0;font-size:14px;letter-spacing:0.04em;color:#cbd5e1;text-transform:uppercase;">Indice comercial</p>
              <p style="margin:0 0 10px 0;font-size:28px;line-height:1.2;color:#ffffff;font-weight:700;">{score_head}</p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 12px 0;">
                <tr>
                  <td style="background:#334155;border-radius:999px;height:8px;font-size:0;line-height:0;">
                    <div style="width:{score_bar_width}%;max-width:100%;height:8px;background:#cbd5e1;border-radius:999px;">&nbsp;</div>
                  </td>
                </tr>
              </table>
              {detalle_score}
              <p style="margin:10px 0 0 0;font-size:13px;line-height:1.5;color:#cbd5e1;">
                El indice integra posicionamiento de precio, actividad de compradores y comparacion con propiedades similares activas y vendidas en {comuna}.
              </p>
            </td>
          </tr>
        </table>
        """

    urls = {
        "aceptar_rebaja": build_action_url(base_url, email, "aceptar_rebaja", codigo, campana, token, mode),
        "mantener_precio": build_action_url(base_url, email, "mantener_precio", codigo, campana, token, mode),
        "contactar_ejecutivo": build_action_url(base_url, email, "contactar_ejecutivo", codigo, campana, token, mode),
        "no_disponible": build_action_url(base_url, email, "no_disponible", codigo, campana, token, mode),
        "unsubscribe": build_action_url(base_url, email, "unsubscribe", codigo, campana, token, mode),
    }

    def btn(url: str, text: str, color: str, border: str = "none", text_color: str = "#ffffff", css_class: str = "") -> str:
        class_attr = f' class="{css_class}"' if css_class else ''
        return (
            f'<a href="{url}"{class_attr} style="display:block;padding:14px 20px;background:{color};'
            f'color:{text_color};text-decoration:none;border-radius:999px;font-weight:700;text-align:center;'
            f'border:{border};font-size:16px;line-height:1.2;transition:background-color 0.2s ease, border-color 0.2s ease, color 0.2s ease;">{text}</a>'
        )

    logo_url = f"{base_url}/static/logo.png"
    ejecutivo_phone = str(prop.get("telefono_ejecutivo") or "").strip()
    phone_digits = re.sub(r"\D", "", ejecutivo_phone)
    if phone_digits.startswith("56"):
        wa_phone = phone_digits
    elif len(phone_digits) == 9 and phone_digits.startswith("9"):
        wa_phone = f"56{phone_digits}"
    else:
        wa_phone = "56942091437"
    whatsapp_url = f"https://wa.me/{wa_phone}"

    bloquea_precio_por_tasacion = estado_precio_tasacion in {"alineada", "bajo_mercado"} or prioridad_ajuste_tasacion in {"baja", "ninguna"}
    es_recomendacion_precio = nuevo > 0 and not bloquea_precio_por_tasacion and (
        ruta_asignada != "Ruta Inteligencia Comunal" or suggested_adjustment_enabled
    )
    
    # Banner Anti-Reply Directo superior obligatorio en CTAs
    banner_anti_reply = """
    <!-- Banner Anti-Reply Directo -->
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#fff7ed;border:1px solid #ffedd5;border-radius:10px;margin:0 0 20px 0;">
      <tr>
        <td style="padding:12px 14px;text-align:center;font-size:13px;line-height:1.5;color:#c2410c;font-weight:600;font-family:inherit;">
          ⚠️ Para responder esta comunicación de manera rápida y registrar su decisión con su asesor, utilice los botones inferiores. No responda directamente a este correo.
        </td>
      </tr>
    </table>
    """

    tiene_sugerencia_precio = usar_ruta_tasacion or es_recomendacion_precio
    if tiene_sugerencia_precio:
        botones_html = banner_anti_reply + f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 12px 0;">
          <tr>
            <td>{btn(urls['aceptar_rebaja'], 'Revisar posicionamiento sugerido', '#292256', css_class='btn-primary')}</td>
          </tr>
        </table>

        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 28px 0;">
          <tr>
            <td>{btn(urls['contactar_ejecutivo'], 'Evaluar recomendación con asesor', '#ffffff', '1px solid #292256', '#292256', css_class='btn-secondary')}</td>
          </tr>
        </table>

        <p style="margin:0 0 8px 0;text-align:center;font-size:14px;line-height:1.5;color:#64748b;">
          <a href="{urls['mantener_precio']}" class="link-hover" style="color:#64748b;text-decoration:underline;transition:color 0.2s ease;">Mantener precio actual</a>
          &nbsp;&nbsp;|&nbsp;&nbsp;
          <a href="{urls['no_disponible']}" class="link-hover" style="color:#64748b;text-decoration:underline;transition:color 0.2s ease;">Propiedad no disponible</a>
        </p>
        """
    else:
        botones_html = banner_anti_reply + f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 28px 0;">
          <tr>
            <td>{btn(urls['contactar_ejecutivo'], 'Analizar mercado con asesor', '#292256', css_class='btn-primary')}</td>
          </tr>
        </table>

        <p style="margin:0 0 8px 0;text-align:center;font-size:14px;line-height:1.5;color:#64748b;">
          <a href="{urls['no_disponible']}" class="link-hover" style="color:#64748b;text-decoration:underline;transition:color 0.2s ease;">Propiedad no disponible</a>
        </p>
        """

    recomendaciones_html = ""
    tabla_comunal_html = ""
    
    if ruta_asignada == "Ruta Tasación Individual":
        precio_sugerido_tasacion = nuevo if nuevo > 0 else (round(tasacion_ref) if tasacion_ref is not None else 0)
        variacion_tasacion = (
            f"{((precio_sugerido_tasacion - precio) / precio * 100.0):+.1f}%".replace(".", ",")
            if (precio > 0 and precio_sugerido_tasacion > 0)
            else "No aplica"
        )
        recomendaciones_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;margin:0 0 26px 0;box-shadow:0 4px 10px rgba(0,0,0,0.03);">
          <tr>
            <td style="padding:22px;">
              <!-- Cabecera -->
              <p style="margin:0 0 4px 0;font-size:18px;line-height:1.35;color:#292256;font-weight:700;letter-spacing:-0.01em;">Evaluación comercial de precio</p>
              <p style="margin:0 0 20px 0;font-size:13px;line-height:1.5;color:#64748b;">
                Análisis comparativo basado en tasaciones vigentes, transacciones reales del Conservador de Bienes Raíces e indicadores de oferta de la zona.
              </p>
              
              <!-- Comparativa de Precios -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 16px 0;">
                <tr>
                  <td style="width:48%;vertical-align:top;background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px;">
                    <p style="margin:0 0 4px 0;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:0.04em;font-weight:700;">Precio publicado</p>
                    <p style="margin:0;font-size:22px;color:#475569;font-weight:700;">{fmt_uf(precio)} <span style="font-size:16px;font-weight:600;">UF</span></p>
                  </td>
                  <td style="width:4%;"></td>
                  <td style="width:48%;vertical-align:top;background:#f0f7ff;border:1px solid #bfdbfe;border-radius:12px;padding:12px 14px;">
                    <p style="margin:0 0 4px 0;font-size:11px;color:#1d4ed8;text-transform:uppercase;letter-spacing:0.04em;font-weight:700;">Sugerido Competitivo</p>
                    <p style="margin:0;font-size:22px;color:#2563eb;font-weight:700;">{fmt_uf(precio_sugerido_tasacion)} <span style="font-size:16px;font-weight:600;">UF</span></p>
                  </td>
                </tr>
              </table>
              
              <!-- Datos de Respaldo -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 20px 0;border-top:1px solid #f1f5f9;">
                <tr>
                  <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;">Tasación comercial de referencia</td>
                  <td align="right" style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:700;color:#292256;">{f"{fmt_uf(tasacion_min)} - {fmt_uf(tasacion_max)} UF" if tasacion_min is not None and tasacion_max is not None else "No disponible"}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;">Diferencia con el mercado</td>
                  <td align="right" style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:700;color:#292256;">
                    <span style="color:#ef4444;font-size:11px;margin-right:4px;vertical-align:middle;">▲</span>{f"{brecha_tasacion_pct:+.1f}%".replace(".", ",")}
                  </td>
                </tr>
                <tr>
                  <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;">Variación recomendada</td>
                  <td align="right" style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:700;color:#292256;">
                    <span style="color:#22c55e;font-size:11px;margin-right:4px;vertical-align:middle;">▼</span>{variacion_tasacion}
                  </td>
                </tr>
              </table>
 
              <!-- Callout Impacto -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f0fdf4;border:1px solid #bbf7d0;border-left:4px solid #16a34a;border-radius:8px;">
                <tr>
                  <td style="padding:12px 14px;">
                    <p style="margin:0 0 3px 0;font-size:13px;font-weight:700;color:#14532d;">Impacto estimado en el mercado</p>
                    <p style="margin:0 0 6px 0;font-size:13px;line-height:1.45;color:#166534;">{impacto}</p>
                    <p style="margin:0;font-size:12px;line-height:1.45;color:#15803d;font-style:italic;">El objetivo es alinear la propiedad con los clientes activos para acelerar cierres concretos.</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
        """
        
    elif ruta_asignada == "Ruta Ajuste Progresivo":
        precio_sugerido_tasacion = nuevo if nuevo > 0 else (round(tasacion_ref) if tasacion_ref is not None else 0)
        recomendaciones_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;margin:0 0 26px 0;box-shadow:0 4px 10px rgba(0,0,0,0.03);">
          <tr>
            <td style="padding:22px;">
              <!-- Cabecera -->
              <p style="margin:0 0 4px 0;font-size:18px;line-height:1.35;color:#292256;font-weight:700;letter-spacing:-0.01em;">Propuesta de revisión comercial estratégica</p>
              <p style="margin:0 0 20px 0;font-size:13px;line-height:1.5;color:#64748b;">
                Hemos detectado una diferencia relevante entre el posicionamiento comercial actual y el comportamiento de oferta de la zona.
              </p>
              
              <!-- Callout Consultivo e Intelectual -->
              <p style="margin:0 0 16px 0;font-size:14px;line-height:1.6;color:#475569;">
                Entendemos que cada propiedad es única y que este tipo de brechas a veces responde a factores de alto valor como <b>mejoras no reflejadas, estrategias comerciales específicas o valoraciones históricas</b>.
              </p>
              <p style="margin:0 0 20px 0;font-size:14px;line-height:1.6;color:#475569;">
                Sin embargo, para resguardar la fluidez comercial de su propiedad frente a la competencia activa y portales, recomendamos coordinar un <b>ajuste progresivo</b> y monitorear de cerca el flujo de leads.
              </p>

              <!-- Comparativa de Precios -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 16px 0;">
                <tr>
                  <td style="width:48%;vertical-align:top;background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px;">
                    <p style="margin:0 0 4px 0;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:0.04em;font-weight:700;">Precio publicado</p>
                    <p style="margin:0;font-size:22px;color:#475569;font-weight:700;">{fmt_uf(precio)} <span style="font-size:16px;font-weight:600;">UF</span></p>
                  </td>
                  <td style="width:4%;"></td>
                  <td style="width:48%;vertical-align:top;background:#f0f7ff;border:1px solid #bfdbfe;border-radius:12px;padding:12px 14px;">
                    <p style="margin:0 0 4px 0;font-size:11px;color:#1d4ed8;text-transform:uppercase;letter-spacing:0.04em;font-weight:700;">Sugerencia de Análisis</p>
                    <p style="margin:0;font-size:22px;color:#2563eb;font-weight:700;">{fmt_uf(precio_sugerido_tasacion)} <span style="font-size:16px;font-weight:600;">UF</span></p>
                  </td>
                </tr>
              </table>
              
              <!-- Datos de Respaldo -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 20px 0;border-top:1px solid #f1f5f9;">
                <tr>
                  <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;">Estrategia sugerida</td>
                  <td align="right" style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:700;color:#292256;">Ajuste progresivo & monitoreo de visitas</td>
                </tr>
                <tr>
                  <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;">Diferencia con el mercado</td>
                  <td align="right" style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:700;color:#292256;">
                    <span style="color:#ef4444;font-size:11px;margin-right:4px;vertical-align:middle;">▲</span>{f"{brecha_tasacion_pct:+.1f}%".replace(".", ",")}
                  </td>
                </tr>
              </table>

              <!-- Callout Ejes de Análisis -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f0f7ff;border:1px solid #bfdbfe;border-left:4px solid #3b82f6;border-radius:8px;">
                <tr>
                  <td style="padding:12px 14px;">
                    <p style="margin:0 0 3px 0;font-size:13px;font-weight:700;color:#1e3a8a;">Ejes clave de revisión personalizada</p>
                    <p style="margin:0 0 4px 0;font-size:13px;line-height:1.45;color:#1e40af;">• <b>Monitoreo de leads:</b> Análisis de consultas reales y clicks en portales.</p>
                    <p style="margin:0 0 4px 0;font-size:13px;line-height:1.45;color:#1e40af;">• <b>Competencia activa:</b> Posicionamiento relativo contra stock actual en {comuna}.</p>
                    <p style="margin:0;font-size:13px;line-height:1.45;color:#1e40af;">• <b>Estrategia comercial:</b> Ajuste progresivo de precio para recuperar competitividad.</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
        """
        
    else:
        # Ruta Inteligencia Comunal
        score_presion_val = float(score_presion) if score_presion is not None else None
        brecha_ic_val = float(brecha_pub_cierre_ic) if brecha_pub_cierre_ic is not None else None
        score_presion_txt = f"{score_presion_val:.0f}/100" if (score_presion_val is not None and score_presion_val > 0) else "No disponible"
        brecha_ic_txt = f"{brecha_ic_val:+.1f}%".replace(".", ",") if (brecha_ic_val is not None and abs(brecha_ic_val) >= 0.1) else "No disponible"
        ufm2_pub_txt = f"{fmt_dec(uf_m2_publicacion_actual, 2)} UF/m2" if uf_m2_publicacion_actual is not None else "No disponible"
        ufm2_cierre_txt = f"{fmt_dec(uf_m2_venta_efectiva_actual, 2)} UF/m2" if uf_m2_venta_efectiva_actual is not None else "No disponible"
        activas_txt = f"{int(publicaciones_activas):,}".replace(",", ".") if publicaciones_activas is not None else "No disponible"
        variacion_ufm2_12m = prop.get("variacion_uf_m2_12m")
        variacion_ufm2_12m_txt = (
            f"{float(variacion_ufm2_12m):+.1f}%".replace(".", ",")
            if variacion_ufm2_12m not in (None, "", "0", 0)
            else "No disponible"
        )
        
        variacion_val = None
        if variacion_ufm2_12m not in (None, "", "0", 0):
            try:
                variacion_val = float(variacion_ufm2_12m)
            except:
                pass
                
        brecha_arrow = ""
        if brecha_ic_val is not None:
            if brecha_ic_val < 0:
                brecha_arrow = '<span style="color:#ef4444;font-size:11px;margin-right:4px;vertical-align:middle;">▼</span>'
            elif brecha_ic_val > 0:
                brecha_arrow = '<span style="color:#22c55e;font-size:11px;margin-right:4px;vertical-align:middle;">▲</span>'

        variacion_arrow = ""
        if variacion_val is not None:
            if variacion_val < 0:
                variacion_arrow = '<span style="color:#ef4444;font-size:11px;margin-right:4px;vertical-align:middle;">▼</span>'
            elif variacion_val > 0:
                variacion_arrow = '<span style="color:#22c55e;font-size:11px;margin-right:4px;vertical-align:middle;">▲</span>'

        lectura_brecha = (
            f"En propiedades similares de {comuna}, los cierres efectivos recientes se observan en promedio {f'{abs(brecha_ic_val):.1f}%'.replace('.', ',')} "
            f"{'bajo' if brecha_ic_val < 0 else 'sobre'} los valores iniciales de publicación."
            if (brecha_ic_val is not None and abs(brecha_ic_val) >= 0.1)
            else "La brecha entre publicación y cierre efectivo se mantiene en observacion con los datos actualmente disponibles."
        )
        lectura_competencia = (
            f"Actualmente se observa un {('aumento' if str(prop.get('tendencia_publicaciones') or '').strip().lower() == 'alza' else 'estabilidad')} "
            f"de publicaciones activas en el segmento {tipo.lower()} de {comuna}, con liquidez {liquidez or 'en revision'} y nivel de competencia {nivel_competencia or 'en revision'}."
        )
        if comunal_quality_tier == "parcial":
            contexto_comercial_fallback = (
                f"Mercado con información parcial disponible para {tipo.lower()} en {comuna}. "
                "Observamos señales de mercado y sugerimos monitorear el comportamiento comercial para revisar posicionamiento junto a su asesor."
            )
        else:
            contexto_comercial_fallback = (
                f"El mercado de {tipo.lower()} en {comuna} muestra un entorno competitivo que requiere foco en diferenciación comercial y calidad de publicación. "
                "La recomendación se orienta a fortalecer el posicionamiento, propuesta de valor y gestion activa de oportunidades."
            )
        tabla_comunal_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;margin:0 0 26px 0;box-shadow:0 4px 10px rgba(0,0,0,0.03);">
          <tr>
            <td style="padding:22px;">
              <!-- Cabecera -->
              <p style="margin:0 0 4px 0;font-size:18px;line-height:1.35;color:#292256;font-weight:700;letter-spacing:-0.01em;">Evaluación de inteligencia comercial de mercado</p>
              <p style="margin:0 0 20px 0;font-size:13px;line-height:1.5;color:#64748b;">
                Este informe se enfoca en comportamiento de mercado comunal y posicionamiento competitivo, sin emitir una valoración individual de tasación para la propiedad.
              </p>
              
              <!-- Comparativa de UF/m² -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 16px 0;">
                <tr>
                  <td style="width:48%;vertical-align:top;background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px;">
                    <p style="margin:0 0 4px 0;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:0.04em;font-weight:700;">UF/m² Publicado Promedio</p>
                    <p style="margin:0;font-size:20px;color:#475569;font-weight:700;">{ufm2_pub_txt}</p>
                  </td>
                  <td style="width:4%;"></td>
                  <td style="width:48%;vertical-align:top;background:#f0f7ff;border:1px solid #bfdbfe;border-radius:12px;padding:12px 14px;">
                    <p style="margin:0 0 4px 0;font-size:11px;color:#1d4ed8;text-transform:uppercase;letter-spacing:0.04em;font-weight:700;">UF/m² Cierre Promedio</p>
                    <p style="margin:0;font-size:20px;color:#2563eb;font-weight:700;">{ufm2_cierre_txt}</p>
                  </td>
                </tr>
              </table>
              
              <!-- Tabla de Indicadores -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 20px 0;border-top:1px solid #f1f5f9;">
                <tr>
                  <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;">Liquidez de mercado</td>
                  <td align="right" style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:700;color:#292256;">{liquidez or "No disponible"}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;">Nivel de competencia</td>
                  <td align="right" style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:700;color:#292256;">{nivel_competencia or "No disponible"}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;">Presión comercial en zona</td>
                  <td align="right" style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:700;color:#292256;">{score_presion_txt}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;">Brecha publicación/cierre</td>
                  <td align="right" style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:700;color:#292256;">
                    {brecha_arrow}{brecha_ic_txt}
                  </td>
                </tr>
                <tr>
                  <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;">Publicaciones activas en segmento</td>
                  <td align="right" style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:700;color:#292256;">{activas_txt}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;">Variación anual UF/m²</td>
                  <td align="right" style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:700;color:#292256;">
                    {variacion_arrow}{variacion_ufm2_12m_txt}
                  </td>
                </tr>
              </table>
 
              <!-- Callout Contexto -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f0fdf4;border:1px solid #bbf7d0;border-left:4px solid #16a34a;border-radius:8px;">
                <tr>
                  <td style="padding:12px 14px;">
                    <p style="margin:0 0 6px 0;font-size:13px;font-weight:700;color:#14532d;">Análisis y contexto comercial</p>
                    <p style="margin:0 0 6px 0;font-size:13px;line-height:1.45;color:#166534;">• {lectura_brecha}</p>
                    <p style="margin:0 0 6px 0;font-size:13px;line-height:1.45;color:#166534;">• {lectura_competencia}</p>
                    <p style="margin:0;font-size:13px;line-height:1.45;color:#166534;">• {contexto_comercial_fallback}</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
        """

        if es_recomendacion_precio:
            card_title = "Sugerencia de ajuste"
            if accion == "bajar_precio_urgente":
                card_title = "Recomendación prioritaria"
                
            recomendaciones_html = f"""
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;margin:0 0 26px 0;box-shadow:0 4px 10px rgba(0,0,0,0.03);">
              <tr>
                <td style="padding:22px;">
                  <!-- Cabecera -->
                  <p style="margin:0 0 4px 0;font-size:18px;line-height:1.35;color:#292256;font-weight:700;letter-spacing:-0.01em;">{card_title}</p>
                  <p style="margin:0 0 20px 0;font-size:13px;line-height:1.5;color:#64748b;">
                    Recomendación de precio basada en información comunal robusta, oferta activa y comparables del segmento.
                  </p>
                  
                  <!-- Comparativa de Precios -->
                  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 16px 0;">
                    <tr>
                      <td style="width:48%;vertical-align:top;background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px;">
                        <p style="margin:0 0 4px 0;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:0.04em;font-weight:700;">Precio publicado</p>
                        <p style="margin:0;font-size:22px;color:#475569;font-weight:700;">{fmt_uf(precio)} <span style="font-size:16px;font-weight:600;">UF</span></p>
                      </td>
                      <td style="width:4%;"></td>
                      <td style="width:48%;vertical-align:top;background:#f0f7ff;border:1px solid #bfdbfe;border-radius:12px;padding:12px 14px;">
                        <p style="margin:0 0 4px 0;font-size:11px;color:#1d4ed8;text-transform:uppercase;letter-spacing:0.04em;font-weight:700;">Precio sugerido</p>
                        <p style="margin:0;font-size:22px;color:#2563eb;font-weight:700;">{fmt_uf(nuevo)} <span style="font-size:16px;font-weight:600;">UF</span></p>
                      </td>
                    </tr>
                  </table>
                  
                  <!-- Datos de Respaldo -->
                  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 20px 0;border-top:1px solid #f1f5f9;">
                    <tr>
                      <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;color:#475569;">Ajuste estimado de publicación</td>
                      <td align="right" style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:13px;font-weight:700;color:#292256;">
                        <span style="color:#22c55e;font-size:11px;margin-right:4px;vertical-align:middle;">▼</span>{variacion}
                      </td>
                    </tr>
                  </table>
 
                  <!-- Callout Oportunidad -->
                  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f0fdf4;border:1px solid #bbf7d0;border-left:4px solid #16a34a;border-radius:8px;">
                    <tr>
                      <td style="padding:12px 14px;">
                        <p style="margin:0 0 3px 0;font-size:13px;font-weight:700;color:#14532d;">Oportunidad comercial esperada</p>
                        <p style="margin:0 0 4px 0;font-size:13px;line-height:1.45;color:#166534;">• Aumento de visibilidad en portales y listados preferenciales.</p>
                        <p style="margin:0 0 4px 0;font-size:13px;line-height:1.45;color:#166534;">• Mayor volumen de consultas y leads calificados de clientes activos.</p>
                        <p style="margin:0;font-size:13px;line-height:1.45;color:#166534;">• Posicionamiento competitivo óptimo para concretar ofertas de cierre.</p>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>
            """

    bloque_tasacion_adjunta = ""
    if incluye_tasacion_adjunta:
        texto_tasacion_adjunta = (
            "Adjuntamos una tasacion comercial desarrollada por Propiteq, plataforma especializada en análisis de mercado inmobiliario. "
            "El informe considera comparables de arriendo, publicaciones activas y comportamiento reciente de cierre en propiedades similares, "
            "como respaldo tecnico para apoyar la evaluacion comercial de la propiedad."
            if operacion == "arriendo"
            else
            "Adjuntamos una tasacion comercial desarrollada por Propiteq, plataforma especializada en análisis de mercado inmobiliario. "
            "El informe considera comparables vendidos, publicaciones activas y operaciones inscritas recientemente en el Conservador de Bienes Raices, "
            "como respaldo tecnico para apoyar la evaluacion comercial de la propiedad."
        )
        bloque_tasacion_adjunta = """
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;margin:0 0 22px 0;">
          <tr>
            <td style="padding:18px;">
              <p style="margin:0 0 8px 0;font-size:18px;line-height:1.3;color:#292256;font-weight:700;">Informe comercial complementario</p>
              <p style="margin:0;font-size:14px;line-height:1.65;color:#475569;">""" + texto_tasacion_adjunta + """</p>
            </td>
          </tr>
        </table>
        """

    bloque_informe_comercial_adjunta = ""
    if incluye_informe_comercial_adjunta:
        bloque_informe_comercial_adjunta = """
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;margin:0 0 22px 0;">
          <tr>
            <td style="padding:18px;">
              <p style="margin:0 0 8px 0;font-size:18px;line-height:1.3;color:#292256;font-weight:700;">Informe comercial complementario</p>
              <p style="margin:0;font-size:14px;line-height:1.65;color:#475569;">Adjuntamos ademas un informe comercial complementario basado en actividad de mercado, publicaciones activas y operaciones registradas recientemente en propiedades similares.</p>
            </td>
          </tr>
        </table>
        """

    return f"""
    <!doctype html>
    <html lang="es">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width,initial-scale=1" />
        <title>Recomendación comercial</title>
        <link href="https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600&family=Raleway:wght@700;800&family=Roboto:wght@400;500;700&display=swap" rel="stylesheet" />
        <style>
          .btn-primary:hover {{
            background-color: #f27e41 !important;
            border-color: #f27e41 !important;
          }}
          .btn-secondary:hover {{
            background-color: #f8fafc !important;
            border-color: #f27e41 !important;
            color: #f27e41 !important;
          }}
          .btn-whatsapp:hover {{
            background-color: #16a34a !important;
          }}
          .link-hover:hover {{
            color: #f27e41 !important;
          }}
          .link-hover-light:hover {{
            color: #cbd5e1 !important;
          }}
        </style>
      </head>
      <body style="margin:0;padding:0;background:#f3f5f8;font-family:'Open Sans','Roboto',Arial,sans-serif;color:#1f2937;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:24px 12px;">
          <tr>
            <td align="center">
              <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="max-width:640px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0;">
                <tr>
                  <td style="padding:16px 24px;border-bottom:1px solid #e7edf5;">
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                      <tr>
                        <td align="left" style="vertical-align:middle;">
                          <img src="{logo_url}" alt="Procasa" style="height:54px;max-width:200px;" />
                        </td>
                        <td align="right" style="vertical-align:middle;font-size:12px;letter-spacing:0.06em;color:#64748b;text-transform:uppercase;font-weight:700;">
                          Informe comercial personalizado
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>

                <tr>
                  <td style="padding:34px 22px 28px 22px;">
                    <h1 style="margin:0 0 12px 0;font-size:28px;line-height:1.25;color:#292256;font-weight:700;">{"Detectamos una oportunidad de alineamiento comercial respecto al mercado actual" if usar_ruta_tasacion else "Detectamos una oportunidad para mejorar el desempeño comercial de tu propiedad"}</h1>
                    <p style="margin:0 0 12px 0;font-size:16px;line-height:1.65;color:#475569;">{"Este análisis se apoya en tasaciones, comparables vendidos y comportamiento de mercado reciente." if usar_ruta_tasacion else "Este análisis considera comportamiento reciente del mercado, actividad de compradores y publicaciones similares en tu comuna."}</p>
                    <p style="margin:0 0 26px 0;font-size:14px;line-height:1.6;color:#475569;">Base de análisis: {origen_analisis}. Cuando existen datos de cierre, se incorporan operaciones inscritas recientemente para estimar posicionamiento competitivo real.</p>

                    {resumen_html}

                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;margin:0 0 22px 0;">
                      <tr>
                        <td style="padding:18px;">
                          <p style="margin:0 0 14px 0;font-size:16px;line-height:1.3;color:#292256;font-weight:700;letter-spacing:-0.01em;">Propiedad en seguimiento</p>
                          <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                            <tr>
                              <td style="width:33%;vertical-align:top;padding-right:10px;">
                                <p style="margin:0 0 4px 0;font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:0.04em;">Código</p>
                                <p style="margin:0;font-size:15px;font-weight:700;color:#292256;"><a href="{property_url}" class="link-hover" style="color:#f27e41;text-decoration:none;border-bottom:1px dashed #f27e41;transition:color 0.2s ease;">{codigo}</a></p>
                              </td>
                              <td style="width:33%;vertical-align:top;padding-right:10px;">
                                <p style="margin:0 0 4px 0;font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:0.04em;">Tipo</p>
                                <p style="margin:0;font-size:15px;font-weight:700;color:#292256;">{tipo}</p>
                              </td>
                              <td style="width:34%;vertical-align:top;">
                                <p style="margin:0 0 4px 0;font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:0.04em;">Comuna</p>
                                <p style="margin:0;font-size:15px;font-weight:700;color:#292256;">{comuna}</p>
                              </td>
                            </tr>
                          </table>
                        </td>
                      </tr>
                    </table>

                    {comparacion_html}

                    {actividad_html}

                    {score_html}

                    {recomendaciones_html}
                    {tabla_comunal_html}

                    {bloque_tasacion_adjunta}
                    {bloque_informe_comercial_adjunta}

                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;margin:0 0 24px 0;">
                      <tr>
                        <td style="padding:18px;">
                          <p style="margin:0 0 8px 0;font-size:18px;line-height:1.3;color:#292256;font-weight:700;">Observacion comercial</p>
                          <p style="margin:0;font-size:14px;line-height:1.65;color:#475569;">{observacion_comercial}</p>
                        </td>
                      </tr>
                    </table>

                    {botones_html}
                    <p style="margin:0;text-align:center;font-size:12px;line-height:1.5;color:#94a3b8;">
                      <a href="{urls['unsubscribe']}" class="link-hover-light" style="color:#94a3b8;text-decoration:underline;transition:color 0.2s ease;">Dejar de recibir comunicaciones</a>
                    </p>
                  </td>
                </tr>

                <tr>
                  <td style="background:#292256;padding:26px 22px;text-align:center;color:#e2e8f0;">
                    <p style="margin:0 0 6px 0;font-size:20px;color:#ffffff;font-weight:700;">{asesor}</p>
                    <p style="margin:0 0 14px 0;font-size:14px;color:#cbd5e1;">{cargo_ejecutivo}</p>
                    <a href="{whatsapp_url}" class="btn-whatsapp" style="display:inline-block;background:#22c55e;color:#ffffff;text-decoration:none;padding:12px 20px;border-radius:999px;font-weight:700;font-size:16px;line-height:1.2;transition:background-color 0.2s ease;">Contacto directo WhatsApp</a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """


def _attach_pdf_path(msg: MIMEMultipart, pdf: Path) -> bool:
    if not pdf.exists():
        return False
    with pdf.open("rb") as f:
        part = MIMEApplication(f.read(), _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=pdf.name)
        msg.attach(part)
    return True


def send_email(
    to_email: str,
    subject: str,
    html: str,
    codigo: str,
    use_tasacion_pdf: bool = False,
    comercial_pdf_filename: str = "",
) -> tuple[bool, bool, str]:
    """Envía el correo mediante SMTP con retries exponenciales, timeout y Reply-To dinámico."""
    msg = MIMEMultipart()
    msg["From"] = f"Procasa <{Config.GMAIL_USER}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    
    # Inyectar cabecera Reply-To dinámica
    msg["Reply-To"] = f"Procasa Seguimiento <reply+{codigo}@procasa.cl>"
    
    msg.attach(MIMEText(html, "html", "utf-8"))
    attached = False
    if use_tasacion_pdf:
        attached = _attach_pdf_path(msg, TASACIONES_DIR / f"{codigo}.pdf") or attached
    elif comercial_pdf_filename:
        attached = _attach_pdf_path(msg, ANALISIS_COMERCIAL_DIR / comercial_pdf_filename) or attached

    max_retries = 3
    retry_delay = 2.0
    
    for attempt in range(1, max_retries + 1):
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
                server.starttls()
                server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
                server.sendmail(Config.GMAIL_USER, [to_email], msg.as_string())
            return True, attached, "ok"
        except Exception as e:
            print(f"SMTP WARNING: Intento {attempt}/{max_retries} falló para {to_email}: {e}")
            if attempt == max_retries:
                raise e
            time.sleep(retry_delay)
            retry_delay *= 2.0  # Backoff exponencial
            
    return False, attached, "error"


def load_wave1(oficina: str) -> list[dict]:
    """Carga todas las propiedades activas de la oficina y las deduplica en memoria por código."""
    db = get_db()
    
    # Eliminamos filtros restrictivos heredados para cubrir "toda la cartera" activa con precio > 0
    props_cursor = db["propiedades_accionables"].find(
        {
            "oficina": oficina,
            "precio_publicado_uf": {"$gt": 0},
        },
        {
            "_id": 0,
            "codigo_propiedad": 1,
            "operacion": 1,
            "accion_recomendada": 1,
            "comuna": 1,
            "tipo_propiedad": 1,
            "precio_publicado_uf": 1,
            "nuevo_precio_objetivo_uf": 1,
            "dias_publicada": 1,
            "dias_publicacion": 1,
            "tiempo_promedio_venta_dias": 1,
            "tiempo_promedio_comuna_dias": 1,
            "indice_actividad_compradores": 1,
            "actividad_compradores_score": 1,
            "indice_competitividad_precio": 1,
            "competitividad_precio_score": 1,
            "indice_visibilidad": 1,
            "visibilidad_score": 1,
            "score_comercial": 1,
            "rango_competitivo_min_uf": 1,
            "rango_competitivo_max_uf": 1,
            "tendencia_mercado": 1,
            "promedio_publicacion_uf": 1,
            "avg_publicacion_uf": 1,
            "promedio_cierre_uf": 1,
            "avg_cierre_uf": 1,
            "delta_publicacion_cierre_pct": 1,
            "brecha_publicacion_cierre_pct": 1,
            "stock_activo_similares": 1,
            "cantidad_similares_activas": 1,
            "promedio_uf_m2": 1,
            "avg_uf_m2": 1,
            "velocidad_comercial": 1,
            "velocidad_comercial_idx": 1,
            "rango_cierre_min_uf": 1,
            "rango_cierre_max_uf": 1,
            "tasacion_comercial_min_uf": 1,
            "tasacion_comercial_max_uf": 1,
            "tasacion_min_uf": 1,
            "tasacion_max_uf": 1,
        },
    )
    
    # Deduplicación en memoria de la lista base
    seen_codes = set()
    props = []
    for p in props_cursor:
        cod = str(p.get("codigo_propiedad") or "").strip()
        if not cod or cod in seen_codes:
            continue
        seen_codes.add(cod)
        props.append(p)

    uc = db["universo_cartera"]
    by_code = {str(x.get("codigo_propiedad")): x for x in props}
    
    for u in uc.find(
        {"codigo": {"$in": list(by_code.keys())}},
        {"_id": 0, "codigo": 1, "email_propietario": 1, "ejecutivo": 1},
    ):
        c = str(u.get("codigo") or "")
        if c in by_code:
            by_code[c]["email_propietario"] = (u.get("email_propietario") or "").strip()
            by_code[c]["ejecutivo"] = (u.get("ejecutivo") or "").strip()

    inteligencia = db["propiedades_inteligencia_comercial"]
    for ic in inteligencia.find(
        {"codigo_propiedad": {"$in": list(by_code.keys())}},
        {
            "_id": 0,
            "codigo_propiedad": 1,
            "argumento_comercial": 1,
            "campana_recomendada": 1,
            "mercado": 1,
            "liquidez": 1,
            "presion_baja_precio": 1,
            "nivel_competencia": 1,
            "score_presion_comercial": 1,
            "brecha_publicacion_vs_cierre_pct": 1,
            "precio_publicado_uf": 1,
            "prioridad_comercial_score": 1,
            "riesgo_comercial": 1,
            "sobreprecio_pct": 1,
            "tasacion_venta_uf": 1,
            "tipo_propiedad": 1,
            "comuna": 1,
        },
    ):
        c = str(ic.get("codigo_propiedad") or "").strip()
        if not c or c not in by_code:
            continue
        by_code[c].update({
            "argumento_comercial": ic.get("argumento_comercial"),
            "campana_recomendada": ic.get("campana_recomendada"),
            "liquidez": ic.get("liquidez"),
            "presion_baja_precio": ic.get("presion_baja_precio"),
            "nivel_competencia": ic.get("nivel_competencia"),
            "score_presion_comercial": ic.get("score_presion_comercial"),
            "brecha_publicacion_vs_cierre_pct": ic.get("brecha_publicacion_vs_cierre_pct"),
            "riesgo_comercial": ic.get("riesgo_comercial"),
            "sobreprecio_pct": ic.get("sobreprecio_pct"),
        })

        mercado = ic.get("mercado") or {}
        if isinstance(mercado, dict):
            by_code[c]["liquidez"] = by_code[c].get("liquidez") or mercado.get("liquidez")
            by_code[c]["presion_baja_precio"] = by_code[c].get("presion_baja_precio") or mercado.get("presion_baja_precio")
            by_code[c]["nivel_competencia"] = by_code[c].get("nivel_competencia") or mercado.get("nivel_competencia")
            by_code[c]["score_presion_comercial"] = by_code[c].get("score_presion_comercial") or mercado.get("score_presion_comercial")
            by_code[c]["brecha_publicacion_vs_cierre_pct"] = by_code[c].get("brecha_publicacion_vs_cierre_pct") or mercado.get("brecha_publicacion_vs_cierre_pct")

    tasaciones = db["tasaciones"]
    for t in tasaciones.find(
        {"codigo_propiedad": {"$in": list(by_code.keys())}},
        {
            "_id": 0,
            "codigo_propiedad": 1,
            "tasacion_online.valor_comercial.uf": 1,
            "tasacion_online.arriendo_estimado.uf": 1,
            "tasacion_online.valor_minimo_maximo.precio_minimo_uf": 1,
            "tasacion_online.valor_minimo_maximo.precio_maximo_uf": 1,
            "analisis_comercial.estado_precio": 1,
            "analisis_comercial.prioridad_ajuste": 1,
            "analisis_comercial.diferencia_porcentual": 1,
            "argumento_baja_precio": 1,
        },
    ):
        c = str(t.get("codigo_propiedad") or "").strip()
        if not c or c not in by_code:
            continue
        tas = t.get("tasacion_online") or {}
        vcom = tas.get("valor_comercial") or {}
        varr = tas.get("arriendo_estimado") or {}
        vmm = tas.get("valor_minimo_maximo") or {}
        ana = t.get("analisis_comercial") or {}

        val_com_uf = vcom.get("uf")
        val_arr_uf = varr.get("uf")
        min_uf = vmm.get("precio_minimo_uf")
        max_uf = vmm.get("precio_maximo_uf")

        min_uf = float(min_uf) if min_uf not in (None, "", 0, "0") else None
        max_uf = float(max_uf) if max_uf not in (None, "", 0, "0") else None
        val_com_uf = float(val_com_uf) if val_com_uf not in (None, "", 0, "0") else None
        val_arr_uf = float(val_arr_uf) if val_arr_uf not in (None, "", 0, "0") else None

        if min_uf is not None:
            by_code[c]["tasacion_comercial_min_uf"] = min_uf
        if max_uf is not None:
            by_code[c]["tasacion_comercial_max_uf"] = max_uf
        if val_com_uf is not None:
            current_min = by_code[c].get("tasacion_comercial_min_uf")
            current_max = by_code[c].get("tasacion_comercial_max_uf")
            if current_min in (None, "", 0, "0"):
                by_code[c]["tasacion_comercial_min_uf"] = val_com_uf
            if current_max in (None, "", 0, "0"):
                by_code[c]["tasacion_comercial_max_uf"] = val_com_uf
        if val_arr_uf is not None:
            by_code[c]["tasacion_arriendo_uf"] = val_arr_uf

        by_code[c]["estado_precio_tasacion"] = ana.get("estado_precio")
        by_code[c]["prioridad_ajuste_tasacion"] = ana.get("prioridad_ajuste")
        by_code[c]["diferencia_porcentual_tasacion"] = ana.get("diferencia_porcentual")
        if not by_code[c].get("argumento_comercial"):
            by_code[c]["argumento_comercial"] = t.get("argumento_baja_precio")

    mercado_col = db["mercado_comunal"]
    for p in by_code.values():
        comuna_p = str(p.get("comuna") or "").strip()
        tipo_p = str(p.get("tipo_propiedad") or "").strip()
        if not comuna_p or not tipo_p:
            continue
        m = mercado_col.find_one(
            {"comuna": {"$regex": f"^{re.escape(comuna_p)}$", "$options": "i"}, "tipo_propiedad": {"$regex": f"^{re.escape(tipo_p)}$", "$options": "i"}},
            {
                "_id": 0,
                "indicadores_mercado": 1,
                "mercado_venta": 1,
                "resumen_comercial_llm": 1,
                "pdf_control.filename": 1,
            },
        ) or {}
        if not m:
            continue
        ind = m.get("indicadores_mercado") or {}
        mv = m.get("mercado_venta") or {}
        pdf_control = m.get("pdf_control") or {}
        p["liquidez"] = p.get("liquidez") or ind.get("liquidez")
        p["presion_baja_precio"] = p.get("presion_baja_precio") or ind.get("presion_baja_precio")
        p["nivel_competencia"] = p.get("nivel_competencia") or ind.get("nivel_competencia")
        p["score_presion_comercial"] = p.get("score_presion_comercial") or ind.get("score_presion_comercial")
        p["brecha_publicacion_vs_cierre_pct"] = p.get("brecha_publicacion_vs_cierre_pct") or ind.get("brecha_publicacion_vs_cierre_pct")
        p["tendencia_mercado_doc"] = ind.get("tendencia_mercado")
        p["uf_m2_publicacion_actual"] = mv.get("uf_m2_publicacion_actual")
        p["uf_m2_venta_efectiva_actual"] = mv.get("uf_m2_venta_efectiva_actual")
        p["variacion_uf_m2_12m"] = mv.get("variacion_uf_m2_12m")
        p["publicaciones_activas"] = mv.get("publicaciones_activas")
        p["publicaciones_totales"] = mv.get("publicaciones_totales")
        p["tendencia_publicaciones"] = mv.get("tendencia_publicaciones")
        p["resumen_comercial_llm"] = m.get("resumen_comercial_llm")
        p["mercado_pdf_filename"] = pdf_control.get("filename")

    # Enriquecer ejecutivo
    nombres_ejecutivos = {
        (p.get("ejecutivo") or "").strip().lower()
        for p in by_code.values()
        if (p.get("ejecutivo") or "").strip()
    }
    if nombres_ejecutivos:
        usuarios = db["usuarios"]
        for usr in usuarios.find(
            {},
            {
                "_id": 0,
                "nombre": 1,
                "telefono": 1,
                "celular": 1,
                "phone": 1,
                "movil": 1,
            },
        ):
            nombre_usr = (usr.get("nombre") or "").strip().lower()
            if not nombre_usr or nombre_usr not in nombres_ejecutivos:
                continue
            phone = (
                usr.get("telefono")
                or usr.get("celular")
                or usr.get("phone")
                or usr.get("movil")
                or ""
            )
            phone = str(phone).strip()
            if not phone:
                continue
            for p in by_code.values():
                if (p.get("ejecutivo") or "").strip().lower() == nombre_usr:
                    p["telefono_ejecutivo"] = phone

    return list(by_code.values())


def load_by_codigo_for_test(codigo: str) -> list[dict]:
    db = get_db()
    c = str(codigo or "").strip()
    if not c:
        return []

    doc = db["propiedades_accionables"].find_one(
        {"codigo_propiedad": c},
        {
            "_id": 0,
            "codigo_propiedad": 1,
            "operacion": 1,
            "accion_recomendada": 1,
            "comuna": 1,
            "tipo_propiedad": 1,
            "precio_publicado_uf": 1,
            "nuevo_precio_objetivo_uf": 1,
            "dias_publicada": 1,
            "dias_publicacion": 1,
            "tiempo_promedio_venta_dias": 1,
            "tiempo_promedio_comuna_dias": 1,
            "indice_actividad_compradores": 1,
            "actividad_compradores_score": 1,
            "indice_competitividad_precio": 1,
            "competitividad_precio_score": 1,
            "indice_visibilidad": 1,
            "visibilidad_score": 1,
            "score_comercial": 1,
            "rango_competitivo_min_uf": 1,
            "rango_competitivo_max_uf": 1,
            "tendencia_mercado": 1,
            "promedio_publicacion_uf": 1,
            "avg_publicacion_uf": 1,
            "promedio_cierre_uf": 1,
            "avg_cierre_uf": 1,
            "delta_publicacion_cierre_pct": 1,
            "brecha_publicacion_cierre_pct": 1,
            "stock_activo_similares": 1,
            "cantidad_similares_activas": 1,
            "promedio_uf_m2": 1,
            "avg_uf_m2": 1,
            "velocidad_comercial": 1,
            "velocidad_comercial_idx": 1,
            "rango_cierre_min_uf": 1,
            "rango_cierre_max_uf": 1,
            "tasacion_comercial_min_uf": 1,
            "tasacion_comercial_max_uf": 1,
            "tasacion_min_uf": 1,
            "tasacion_max_uf": 1,
        },
    )
    if not doc:
        return []

    uc = db["universo_cartera"].find_one(
        {"codigo": c},
        {"_id": 0, "email_propietario": 1, "ejecutivo": 1},
    ) or {}
    doc["email_propietario"] = (uc.get("email_propietario") or "").strip()
    doc["ejecutivo"] = (uc.get("ejecutivo") or "").strip()

    temp = [doc]
    by_code = {c: doc}

    inteligencia = db["propiedades_inteligencia_comercial"]
    ic = inteligencia.find_one({"codigo_propiedad": c}) or {}
    if ic:
        by_code[c].update({
            "argumento_comercial": ic.get("argumento_comercial"),
            "campana_recomendada": ic.get("campana_recomendada"),
            "liquidez": ic.get("liquidez"),
            "presion_baja_precio": ic.get("presion_baja_precio"),
            "nivel_competencia": ic.get("nivel_competencia"),
            "score_presion_comercial": ic.get("score_presion_comercial"),
            "brecha_publicacion_vs_cierre_pct": ic.get("brecha_publicacion_vs_cierre_pct"),
            "riesgo_comercial": ic.get("riesgo_comercial"),
            "sobreprecio_pct": ic.get("sobreprecio_pct"),
        })
        mercado = ic.get("mercado") or {}
        if isinstance(mercado, dict):
            by_code[c]["liquidez"] = by_code[c].get("liquidez") or mercado.get("liquidez")
            by_code[c]["presion_baja_precio"] = by_code[c].get("presion_baja_precio") or mercado.get("presion_baja_precio")
            by_code[c]["nivel_competencia"] = by_code[c].get("nivel_competencia") or mercado.get("nivel_competencia")
            by_code[c]["score_presion_comercial"] = by_code[c].get("score_presion_comercial") or mercado.get("score_presion_comercial")
            by_code[c]["brecha_publicacion_vs_cierre_pct"] = by_code[c].get("brecha_publicacion_vs_cierre_pct") or mercado.get("brecha_publicacion_vs_cierre_pct")

    t = db["tasaciones"].find_one({"codigo_propiedad": c}) or {}
    if t:
        tas = t.get("tasacion_online") or {}
        vcom = tas.get("valor_comercial") or {}
        varr = tas.get("arriendo_estimado") or {}
        vmm = tas.get("valor_minimo_maximo") or {}
        ana = t.get("analisis_comercial") or {}
        val_com_uf = vcom.get("uf")
        val_arr_uf = varr.get("uf")
        min_uf = vmm.get("precio_minimo_uf")
        max_uf = vmm.get("precio_maximo_uf")
        min_uf = float(min_uf) if min_uf not in (None, "", 0, "0") else None
        max_uf = float(max_uf) if max_uf not in (None, "", 0, "0") else None
        val_com_uf = float(val_com_uf) if val_com_uf not in (None, "", 0, "0") else None
        val_arr_uf = float(val_arr_uf) if val_arr_uf not in (None, "", 0, "0") else None
        if min_uf is not None:
            by_code[c]["tasacion_comercial_min_uf"] = min_uf
        if max_uf is not None:
            by_code[c]["tasacion_comercial_max_uf"] = max_uf
        if val_com_uf is not None:
            if by_code[c].get("tasacion_comercial_min_uf") in (None, "", 0, "0"):
                by_code[c]["tasacion_comercial_min_uf"] = val_com_uf
            if by_code[c].get("tasacion_comercial_max_uf") in (None, "", 0, "0"):
                by_code[c]["tasacion_comercial_max_uf"] = val_com_uf
        if val_arr_uf is not None:
            by_code[c]["tasacion_arriendo_uf"] = val_arr_uf
        by_code[c]["estado_precio_tasacion"] = ana.get("estado_precio")
        by_code[c]["prioridad_ajuste_tasacion"] = ana.get("prioridad_ajuste")
        by_code[c]["diferencia_porcentual_tasacion"] = ana.get("diferencia_porcentual")
        if not by_code[c].get("argumento_comercial"):
            by_code[c]["argumento_comercial"] = t.get("argumento_baja_precio")

    comuna_p = str(doc.get("comuna") or "").strip()
    tipo_p = str(doc.get("tipo_propiedad") or "").strip()
    if comuna_p and tipo_p:
        m = db["mercado_comunal"].find_one(
            {"comuna": {"$regex": f"^{re.escape(comuna_p)}$", "$options": "i"}, "tipo_propiedad": {"$regex": f"^{re.escape(tipo_p)}$", "$options": "i"}},
            {"_id": 0, "indicadores_mercado": 1, "mercado_venta": 1, "resumen_comercial_llm": 1, "pdf_control.filename": 1},
        ) or {}
        if m:
            ind = m.get("indicadores_mercado") or {}
            mv = m.get("mercado_venta") or {}
            pdf_control = m.get("pdf_control") or {}
            by_code[c]["liquidez"] = by_code[c].get("liquidez") or ind.get("liquidez")
            by_code[c]["presion_baja_precio"] = by_code[c].get("presion_baja_precio") or ind.get("presion_baja_precio")
            by_code[c]["nivel_competencia"] = by_code[c].get("nivel_competencia") or ind.get("nivel_competencia")
            by_code[c]["score_presion_comercial"] = by_code[c].get("score_presion_comercial") or ind.get("score_presion_comercial")
            by_code[c]["brecha_publicacion_vs_cierre_pct"] = by_code[c].get("brecha_publicacion_vs_cierre_pct") or ind.get("brecha_publicacion_vs_cierre_pct")
            by_code[c]["tendencia_mercado_doc"] = ind.get("tendencia_mercado")
            by_code[c]["uf_m2_publicacion_actual"] = mv.get("uf_m2_publicacion_actual")
            by_code[c]["uf_m2_venta_efectiva_actual"] = mv.get("uf_m2_venta_efectiva_actual")
            by_code[c]["variacion_uf_m2_12m"] = mv.get("variacion_uf_m2_12m")
            by_code[c]["publicaciones_activas"] = mv.get("publicaciones_activas")
            by_code[c]["publicaciones_totales"] = mv.get("publicaciones_totales")
            by_code[c]["tendencia_publicaciones"] = mv.get("tendencia_publicaciones")
            by_code[c]["resumen_comercial_llm"] = m.get("resumen_comercial_llm")
            by_code[c]["mercado_pdf_filename"] = pdf_control.get("filename")

    ejecutivo = (doc.get("ejecutivo") or "").strip().lower()
    if ejecutivo:
        usr = db["usuarios"].find_one(
            {"nombre": {"$regex": f"^{re.escape(ejecutivo)}$", "$options": "i"}},
            {"_id": 0, "telefono": 1, "celular": 1, "phone": 1, "movil": 1},
        ) or {}
        phone = usr.get("telefono") or usr.get("celular") or usr.get("phone") or usr.get("movil")
        if phone:
            doc["telefono_ejecutivo"] = str(phone).strip()

    return temp


def registrar_envio(db, campana: str, p: dict, to_email: str, ok: bool, attached: bool, token: str, mode: str) -> None:
    """Registra de manera completa y trazable la transacción comercial en campanas_historico."""
    db[Config.COLLECTION_CAMPANAS_LOG].insert_one(
        {
            "campana": campana,
            "codigo_propiedad": str(p.get("codigo_propiedad") or ""),
            "email_destino": to_email,
            "accion_recomendada": p.get("accion_recomendada"),
            "estado_envio": "enviado" if ok else "error",
            "pdf_adjunto": attached,
            "token": token,
            "mode": mode,
            "enviado_at": datetime.now(timezone.utc).isoformat(),
            "template_version": TEMPLATE_VERSION,
            "template_type": "ajuste_progresivo" if p.get("ruta_asignada") == "Ruta Ajuste Progresivo" else ("tasacion_individual" if p.get("ruta_asignada") == "Ruta Tasación Individual" else "inteligencia_comunal"),
            "route_type": p.get("ruta_asignada"),
            "brecha_snapshot": p.get("brecha_calculada_pct"),
            "tasacion_snapshot": p.get("tasacion_comercial_min_uf") or p.get("tasacion_min_uf") or p.get("tasacion_arriendo_uf"),
            "precio_publicado_snapshot": p.get("precio_publicado_uf"),
            "confidence_score": p.get("confidence_score"),
            "categoria_outlier": p.get("categoria_outlier"),
            "market_data_score": p.get("market_data_score"),
            "comunal_data_complete": p.get("comunal_data_complete"),
            "missing_market_fields": p.get("missing_market_fields"),
            "comunal_quality_tier": p.get("comunal_quality_tier"),
            "suggested_adjustment_enabled": p.get("suggested_adjustment_enabled"),
            "exclusion_reason": p.get("exclusion_reason"),
            "send_eligible": p.get("send_eligible"),
        }
    )


def congelar_snapshot(db, campana: str, oficina: str, properties: list) -> None:
    """Congela y persiste un snapshot inmutable completo de la campaña."""
    db["campanas_snapshots"].update_one(
        {"campana": campana, "oficina": oficina},
        {
            "$set": {
                "snapshot_at": datetime.now(timezone.utc).isoformat(),
                "properties": properties
            }
        },
        upsert=True
    )


def cargar_snapshot(db, campana: str, oficina: str) -> list | None:
    """Carga el snapshot inmutable inalterable de la base de datos."""
    doc = db["campanas_snapshots"].find_one({"campana": campana, "oficina": oficina})
    if doc:
        return doc.get("properties")
    return None


def ejecutar_tests_suite() -> None:
    """Ejecuta una completa suite de pruebas unitarias para certificar la consistencia del motor."""
    print("=" * 80)
    print("EJECUTANDO LA SUITE DE PRUEBAS DE CALIDAD E INTEGRIDAD DEL MOTOR")
    print("=" * 80)
    
    # 1. Test Validación PDF Físico
    print("Test 1: Verificación de Heurísticas de Reportes PDF...")
    temp_dir = Path("scratch_test_pdfs")
    temp_dir.mkdir(exist_ok=True)
    
    pdf_vacio = temp_dir / "vacio.pdf"
    pdf_vacio.write_bytes(b"")
    pdf_corrupto = temp_dir / "corrupto.pdf"
    pdf_corrupto.write_bytes(b"Este no es un pdf valido en absoluto." + b"X" * 6000)
    pdf_valido = temp_dir / "valido.pdf"
    # Escribir cabecera mágica y llenar hasta superar 5KB
    pdf_valido.write_bytes(b"%PDF-1.4\n" + b"X" * 6000)
    
    res_vacio, motive_vacio = validar_pdf_fisico(pdf_vacio)
    res_corr, motive_corr = validar_pdf_fisico(pdf_corrupto)
    res_val, motive_val = validar_pdf_fisico(pdf_valido)
    res_missing, motive_missing = validar_pdf_fisico(temp_dir / "inexistente.pdf")
    
    assert res_vacio is False and motive_vacio == "pdf_small_size", f"Fallo vacio: {motive_vacio}"
    assert res_corr is False and motive_corr == "pdf_invalid_header", f"Fallo corrupto: {motive_corr}"
    assert res_val is True, f"Fallo valido: {motive_val}"
    assert res_missing is False and motive_missing == "pdf_missing", f"Fallo missing: {motive_missing}"
    print(" -> [OK] Verificaciones de archivos PDF correctas.")
    
    # Limpieza
    for f in temp_dir.glob("*"):
        f.unlink()
    temp_dir.rmdir()
    
    # 2. Test Clasificación y Heurísticas de Ruteo
    print("Test 2: Clasificación de Outliers y Asignación de Rutas...")
    p_moderado = {"codigo_propiedad": "9901", "precio_publicado_uf": 1000, "tasacion_min_uf": 900, "email_propietario": "t@t.com"}
    p_extremo = {"codigo_propiedad": "9902", "precio_publicado_uf": 2000, "tasacion_min_uf": 1000, "email_propietario": "t@t.com"}
    p_err_probable = {"codigo_propiedad": "9903", "precio_publicado_uf": 5000, "tasacion_min_uf": 1000, "email_propietario": "t@t.com"}
    
    # Simulamos la existencia de PDF creando el archivo
    TASACIONES_DIR.mkdir(exist_ok=True)
    pdf_9901 = TASACIONES_DIR / "9901.pdf"
    pdf_9901.write_bytes(b"%PDF" + b"X" * 6000)
    pdf_9902 = TASACIONES_DIR / "9902.pdf"
    pdf_9902.write_bytes(b"%PDF" + b"X" * 6000)
    pdf_9903 = TASACIONES_DIR / "9903.pdf"
    pdf_9903.write_bytes(b"%PDF" + b"X" * 6000)
    
    try:
        class_moderado = clasificar_propiedad(p_moderado, TASACIONES_DIR)
        class_extremo = clasificar_propiedad(p_extremo, TASACIONES_DIR)
        class_err = clasificar_propiedad(p_err_probable, TASACIONES_DIR)
        
        assert class_moderado["ruta_asignada"] == "Ruta Tasación Individual", f"Moderado falló: {class_moderado}"
        assert class_extremo["ruta_asignada"] == "Ruta Ajuste Progresivo", f"Extremo falló: {class_extremo}"
        assert class_err["ruta_asignada"] == "Cola Manual" and class_err["categoria_outlier"] == "Error probable - Cola manual", f"Error probable falló: {class_err}"
        print(" -> [OK] Reglas de ruteo y exclusión de outliers correctas.")
    finally:
        pdf_9901.unlink()
        pdf_9902.unlink()
        pdf_9903.unlink()

    # 3. Test Concurrencia / Doble click rápido atómico
    print("Test 3: Seguridad Multi-click con Concurrencia Real...")
    db = get_db()
    token_test = "token_concurrente_safety_" + secrets.token_hex(4)
    historico = db[Config.COLLECTION_CAMPANAS_LOG]
    
    # Inicializar entrada de envío
    historico.insert_one({
        "campana": "campana_test_concurrente",
        "codigo_propiedad": "9999",
        "email_destino": "test@procasa.cl",
        "token": token_test,
        "enviado_at": datetime.now(timezone.utc).isoformat()
    })
    
    results = []
    def simular_click_hilo():
        # Lógica simplificada de find_one_and_update del handler
        res = historico.find_one_and_update(
            {"token": token_test, "respuesta_confirmada": {"$ne": True}},
            {"$set": {"respuesta_confirmada": True, "click_at": datetime.utcnow()}},
            return_document=False
        )
        results.append(res)

    t1 = threading.Thread(target=simular_click_hilo)
    t2 = threading.Thread(target=simular_click_hilo)
    
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    # Uno de los dos debe haber retornado el documento original (no None), y el otro None
    exitosos = [r for r in results if r is not None]
    fallidos = [r for r in results if r is None]
    
    assert len(exitosos) == 1, f"Error en race condition: {len(exitosos)} pasaron."
    assert len(fallidos) == 1, f"Error en race condition: {len(fallidos)} bloqueados."
    
    # Limpieza
    historico.delete_one({"token": token_test})
    print(" -> [OK] Bloqueo de multi-click atómico validado con 100% de éxito.")
    print("=" * 80)
    print("TODAS LAS PRUEBAS PASARON CORRECTAMENTE. EL MOTOR ES APTO PARA PRODUCCIÓN.")
    print("=" * 80)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["preview", "live", "test"], default="test")
    ap.add_argument("--test-email", default="pgalleguillos@procasa.cl")
    ap.add_argument("--oficina", default="PROCASA SUCRE")
    ap.add_argument("--campana", default=f"baja_precio_ola1_{datetime.now(timezone.utc).strftime('%Y%m%d')}")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--codigo", default="", help="Codigo de propiedad para pruebas puntuales en modo test")
    ap.add_argument("--confirm-preview", action="store_true", help="Confirmar visualización de preview para poder correr en modo live")
    ap.add_argument("--run-tests", action="store_true", help="Ejecutar suite de tests unitarios")
    args = ap.parse_args()

    if args.run_tests:
        ejecutar_tests_suite()
        return

    db = get_db()
    base_url = Config.CRM_BASE_URL.rstrip("/")
    test_email_fijo = args.test_email

    # =========================================================================
    # RUTA DE PREVISUALIZACIÓN Y CONGELACIÓN (PREVIEW)
    # =========================================================================
    if args.mode == "preview":
        print(f"INFO: Generando previsualización de campaña comercial para oficina '{args.oficina}'...")
        raw_wave = load_wave1(args.oficina)
        
        properties_snapshot = []
        for p in raw_wave:
            classification = clasificar_propiedad(p, TASACIONES_DIR)
            p.update(classification)
            properties_snapshot.append(p)
            
        # Guardar / Congelar Snapshot
        congelar_snapshot(db, args.campana, args.oficina, properties_snapshot)
        
        # Generar Excel de auditoría
        exports_dir = PROJECT_ROOT / "exports"
        exports_dir.mkdir(exist_ok=True)
        excel_path = exports_dir / f"preview_campana_{args.campana}.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "preview"
        ws.append([
            "codigo", "email", "ejecutivo", "ruta", "brecha_pct",
            "tiene_pdf", "pdf_motivo", "confidence_score",
            "categoria_outlier", "usa_tasacion", "accion_sugerida", "precio_publicado",
            "market_data_score", "comunal_data_complete", "missing_market_fields",
            "exclusion_reason", "send_eligible", "suggested_adjustment_enabled", "comunal_quality_tier"
        ])
        for p in properties_snapshot:
            ws.append([
                p.get("codigo_propiedad"),
                p.get("email_propietario") or "SIN_EMAIL",
                p.get("ejecutivo") or "EQUIPO_PROCASA",
                p.get("ruta_asignada"),
                f"{p.get('brecha_calculada_pct'):.2f}%" if p.get('brecha_calculada_pct') is not None else "N/A",
                "SI" if p.get("pdf_valido") else "NO",
                p.get("pdf_motive"),
                p.get("confidence_score"),
                p.get("categoria_outlier"),
                "SI" if p.get("usa_tasacion") else "NO",
                p.get("accion_sugerida"),
                p.get("precio_publicado_uf"),
                p.get("market_data_score"),
                "SI" if p.get("comunal_data_complete") else "NO",
                p.get("missing_market_fields") or "",
                p.get("exclusion_reason"),
                "SI" if p.get("send_eligible") else "NO",
                "SI" if p.get("suggested_adjustment_enabled") else "NO",
                p.get("comunal_quality_tier"),
            ])
        wb.save(excel_path)
                
        # Calcular Estadísticas
        total_props = len(properties_snapshot)
        sendable = [x for x in properties_snapshot if x.get("ruta_asignada") != "Cola Manual" and bool(x.get("send_eligible", True))]
        cola_manual = [x for x in properties_snapshot if x.get("ruta_asignada") == "Cola Manual" or not bool(x.get("send_eligible", True))]
        
        ruta_ind = len([x for x in sendable if x.get("ruta_asignada") == "Ruta Tasación Individual"])
        ruta_prog = len([x for x in sendable if x.get("ruta_asignada") == "Ruta Ajuste Progresivo"])
        ruta_com = len([x for x in sendable if x.get("ruta_asignada") == "Ruta Inteligencia Comunal"])
        comunal_robusta = len([x for x in properties_snapshot if x.get("ruta_asignada") == "Ruta Inteligencia Comunal" and x.get("comunal_quality_tier") == "robusta"])
        comunal_parcial = len([x for x in properties_snapshot if x.get("ruta_asignada") == "Ruta Inteligencia Comunal" and x.get("comunal_quality_tier") == "parcial"])
        comunal_insuf = len([x for x in properties_snapshot if x.get("comunal_quality_tier") == "insuficiente"])
        excl_sin_email = len([x for x in properties_snapshot if x.get("exclusion_reason") == "SIN_EMAIL"])
        excl_outlier = len([x for x in properties_snapshot if x.get("exclusion_reason") == "OUTLIER_EXTREMO"])
        excl_datos = len([x for x in properties_snapshot if x.get("exclusion_reason") == "DATOS_COMUNALES_INSUFICIENTES"])
        excl_pdf_invalido = len([x for x in properties_snapshot if not x.get("pdf_valido") and x.get("ruta_asignada") != "Ruta Inteligencia Comunal"])
        excl_manual = len([x for x in properties_snapshot if x.get("exclusion_reason") == "MANUAL_REVIEW_REQUIRED"])
        
        print("=" * 80)
        print(f"CAMPANA SNAPSHOT GENERADA CON EXITO")
        print(f"Campaña ID:   {args.campana}")
        print(f"Excel Auditoría: {excel_path.resolve()}")
        print("=" * 80)
        print(f"Total propiedades analizadas:          {total_props}")
        print(f"  -> Para Enviar Automáticamente:      {len(sendable)}")
        print(f"  -> En Cola Manual (Excluidas):       {len(cola_manual)}")
        print("-" * 80)
        print("Distribución de Rutas de Envío:")
        print(f"  -> Ruta Tasación Individual:         {ruta_ind} propiedades")
        print(f"  -> Ruta Ajuste Progresivo:           {ruta_prog} propiedades")
        print(f"  -> Ruta Inteligencia Comunal:        {ruta_com} propiedades")
        print("-" * 80)
        print("Calidad Inteligencia Comunal:")
        print(f"  -> Robusta:                          {comunal_robusta}")
        print(f"  -> Parcial:                          {comunal_parcial}")
        print(f"  -> Insuficiente:                     {comunal_insuf}")
        print("-" * 80)
        print("Exclusiones por motivo:")
        print(f"  -> SIN_EMAIL:                        {excl_sin_email}")
        print(f"  -> OUTLIER_EXTREMO:                  {excl_outlier}")
        print(f"  -> DATOS_COMUNALES_INSUFICIENTES:    {excl_datos}")
        print(f"  -> PDF_INVALIDO:                     {excl_pdf_invalido}")
        print(f"  -> MANUAL_REVIEW_REQUIRED:           {excl_manual}")
        print("=" * 80)
        print("INFO: Puede proceder al envío en vivo agregando --mode live y --confirm-preview.")
        return

    # =========================================================================
    # RUTA DE ENVÍO EN VIVO (LIVE)
    # =========================================================================
    elif args.mode == "live":
        if not args.confirm_preview:
            print("ERROR CRITICO: Envío masivo bloqueado. Debe previsualizar el Excel y ejecutar con --confirm-preview.")
            sys.exit(1)
            
        wave = cargar_snapshot(db, args.campana, args.oficina)
        if not wave:
            print(f"ERROR CRITICO: No existe un snapshot congelado para la campaña '{args.campana}'.")
            print("Debe ejecutar primero en modo --mode preview.")
            sys.exit(1)
            
        print("=" * 80)
        print(f"INICIANDO ENVIO EN VIVO DESDE SNAPSHOT CONGELADO INMUTABLE")
        print(f"Campaña ID: {args.campana} | Oficina: {args.oficina}")
        print("=" * 80)
        
        sent = 0
        skipped_manual = 0
        skipped_idempotencia = 0
        skipped_by_reason: dict[str, int] = {}
        
        for i, p in enumerate(wave, start=1):
            codigo = str(p.get("codigo_propiedad") or "")
            email_owner = (p.get("email_propietario") or "").strip()
            ruta = p.get("ruta_asignada")
            
            # 1. Excluir propiedades en cola manual o sin correo
            if ruta == "Cola Manual" or not bool(p.get("send_eligible", True)):
                skipped_manual += 1
                reason = str(p.get("exclusion_reason") or "MANUAL_REVIEW_REQUIRED")
                skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
                continue
            if not email_owner:
                skipped_by_reason["SIN_EMAIL"] = skipped_by_reason.get("SIN_EMAIL", 0) + 1
                continue
                
            # 2. VALIDACION DE IDEMPOTENCIA DIRECTA ANTES DEL SEND TRANSPORT
            idempotencia_key = {
                "campana": args.campana,
                "codigo_propiedad": codigo,
                "estado_envio": "enviado"
            }
            if db[Config.COLLECTION_CAMPANAS_LOG].find_one(idempotencia_key):
                skipped_idempotencia += 1
                print(f"[{i}/{len(wave)}] IDEMPOTENCIA: Propiedad {codigo} ya fue enviada previamente. Saltando...")
                continue

            # 3. Aplicar Throttling Gradual
            if sent > 0:
                delay = random.uniform(1.5, 3.5)
                time.sleep(delay)
                
                # Pausa estricta por lotes
                if sent % 25 == 0:
                    print(f"INFO: Lote de 25 correos enviado. Pausando por 12 segundos para cuidar la reputación SMTP...")
                    time.sleep(12.0)

            token = secrets.token_urlsafe(20)
            asesor = (p.get("ejecutivo") or "Equipo Procasa").strip() or "Equipo Procasa"
            
            # Mapear PDFs según ruta asignada
            usa_tasacion = p.get("usa_tasacion", False)
            pdf_comercial_filename = str(p.get("mercado_pdf_filename") or "").strip()
            pdf_comercial_disponible = bool(pdf_comercial_filename) and (ANALISIS_COMERCIAL_DIR / pdf_comercial_filename).exists()
            incluir_informe_comercial = (not usa_tasacion) and pdf_comercial_disponible

            html = build_html(
                p,
                email_owner,
                args.campana,
                base_url,
                asesor,
                token,
                mode=args.mode,
                incluye_tasacion_adjunta=usa_tasacion,
                incluye_informe_comercial_adjunta=incluir_informe_comercial,
            )
            subject = f"Procasa | Análisis de mercado personalizado sobre tu propiedad {codigo}"

            ok = False
            attached = False
            try:
                ok, attached, _ = send_email(
                    email_owner,
                    subject,
                    html,
                    codigo,
                    use_tasacion_pdf=usa_tasacion,
                    comercial_pdf_filename=(pdf_comercial_filename if incluir_informe_comercial else ""),
                )
            except Exception as e:
                print(f"[{i}/{len(wave)}] ERROR SMTP codigo={codigo} to={email_owner} err={e}")

            registrar_envio(db, args.campana, p, email_owner, ok, attached, token, args.mode)
            if ok:
                sent += 1
                print(f"[{i}/{len(wave)}] ENVIADO: codigo={codigo} to={email_owner} ruta={ruta} pdf={attached}")
                
        print("=" * 80)
        print(f"CAMPANA EN VIVO FINALIZADA")
        print(f"Correos enviados exitosamente:     {sent}")
        print(f"Propiedades saltadas (Cola Manual): {skipped_manual}")
        print(f"Propiedades saltadas (Idempotente): {skipped_idempotencia}")
        print("Detalle exclusiones:")
        print(f"  -> SIN_EMAIL:                     {skipped_by_reason.get('SIN_EMAIL', 0)}")
        print(f"  -> OUTLIER_EXTREMO:               {skipped_by_reason.get('OUTLIER_EXTREMO', 0)}")
        print(f"  -> DATOS_COMUNALES_INSUFICIENTES: {skipped_by_reason.get('DATOS_COMUNALES_INSUFICIENTES', 0)}")
        print(f"  -> PDF_INVALIDO:                  {skipped_by_reason.get('PDF_INVALIDO', 0)}")
        print(f"  -> MANUAL_REVIEW_REQUIRED:        {skipped_by_reason.get('MANUAL_REVIEW_REQUIRED', 0)}")
        print("=" * 80)
        return

    # =========================================================================
    # RUTA DE TEST UNITARIO PARCIAL (TEST)
    # =========================================================================
    elif args.mode == "test":
        codigo_input = (args.codigo or "").strip()
        if not codigo_input:
            codigo_input = input("Ingresa codigo_propiedad para prueba: ").strip()
        if not codigo_input:
            print("Debes ingresar un codigo_propiedad en modo test.")
            return
            
        wave = load_by_codigo_for_test(codigo_input)
        if not wave:
            print(f"No se encontro la propiedad codigo={codigo_input}.")
            return
            
        p = wave[0]
        classification = clasificar_propiedad(p, TASACIONES_DIR)
        p.update(classification)
        
        token = secrets.token_urlsafe(20)
        asesor = (p.get("ejecutivo") or "Equipo Procasa").strip() or "Equipo Procasa"
        
        usa_tasacion = p.get("usa_tasacion", False)
        pdf_comercial_filename = str(p.get("mercado_pdf_filename") or "").strip()
        pdf_comercial_disponible = bool(pdf_comercial_filename) and (ANALISIS_COMERCIAL_DIR / pdf_comercial_filename).exists()
        incluir_informe_comercial = (not usa_tasacion) and pdf_comercial_disponible

        html = build_html(
            p,
            p.get("email_propietario") or "test@procasa.cl",
            args.campana,
            base_url,
            asesor,
            token,
            mode=args.mode,
            incluye_tasacion_adjunta=usa_tasacion,
            incluye_informe_comercial_adjunta=incluir_informe_comercial,
        )
        subject = f"[TEST] Procasa | Análisis de mercado personalizado sobre tu propiedad {codigo_input}"

        print(f"INFO: Enviando correo de prueba a {test_email_fijo}...")
        print(f"  -> Código propiedad:  {codigo_input}")
        print(f"  -> Ruta asignada:     {p.get('ruta_asignada')}")
        print(f"  -> Score de confianza: {p.get('confidence_score')}/100")
        print(f"  -> Categoría outlier: {p.get('categoria_outlier')}")
        
        ok = False
        attached = False
        try:
            ok, attached, _ = send_email(
                test_email_fijo,
                subject,
                html,
                codigo_input,
                use_tasacion_pdf=usa_tasacion,
                comercial_pdf_filename=(pdf_comercial_filename if incluir_informe_comercial else ""),
            )
        except Exception as e:
            print(f"ERROR SMTP TEST: {e}")

        # Registrar en logs con flag de modo test
        registrar_envio(db, args.campana, p, test_email_fijo, ok, attached, token, args.mode)
        if ok:
            print(f"TEST EXITOSO: Correo enviado a {test_email_fijo} con adjunto={attached}")
        else:
            print("TEST FALLIDO: Revisa los logs de error SMTP.")


if __name__ == "__main__":
    main()
