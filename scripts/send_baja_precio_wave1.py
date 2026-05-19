#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Envio campana comercial de baja de precio (prueba + ola 1)."""

from __future__ import annotations

import argparse
import secrets
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus
import re

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import Config
from chatbot.storage import get_db

TASACIONES_DIR = Path(r"C:/Users/pgall/Desktop/Tasaciones")


def build_action_url(base_url: str, email: str, accion: str, codigo: str, campana: str, token: str, mode: str = "live") -> str:
    return (
        f"{base_url}/campana/respuesta?email={quote_plus(email)}"
        f"&accion={quote_plus(accion)}&codigos={quote_plus(codigo)}"
        f"&campana={quote_plus(campana)}&token={quote_plus(token)}&mode={quote_plus(mode)}"
    )


def _texto_por_accion(accion: str) -> tuple[str, str, str, str]:
    if accion == "bajar_precio_urgente":
        return (
            "Recomendacion prioritaria",
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
            "Revision de publicacion",
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


def build_html(prop: dict, email: str, campana: str, base_url: str, asesor: str, token: str, mode: str = "live", incluye_tasacion_adjunta: bool = False) -> str:
    codigo = str(prop.get("codigo_propiedad") or "")
    property_url = f"https://www.procasa.cl/{codigo}" if codigo else "https://www.procasa.cl"
    comuna = prop.get("comuna") or ""
    tipo = prop.get("tipo_propiedad") or ""
    accion = prop.get("accion_recomendada") or "bajar_precio_sugerida"
    precio = float(prop.get("precio_publicado_uf") or 0)
    nuevo = float(prop.get("nuevo_precio_objetivo_uf") or 0)

    asunto_tag, _, _, _ = _texto_por_accion(accion)
    delta_pct = ((nuevo - precio) / precio * 100.0) if (precio > 0 and nuevo > 0) else None
    variacion = f"{delta_pct:+.1f}%" if delta_pct is not None else "No aplica"
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

    avg_pub_uf = float(avg_pub_uf_raw) if avg_pub_uf_raw is not None else None
    avg_cierre_uf = float(avg_cierre_uf_raw) if avg_cierre_uf_raw is not None else None
    delta_pub_cierre = float(delta_pub_cierre_raw) if delta_pub_cierre_raw is not None else None
    stock_activo = int(stock_activo_raw) if stock_activo_raw is not None else None
    promedio_uf_m2 = float(promedio_uf_m2_raw) if promedio_uf_m2_raw is not None else None
    velocidad_comercial = int(velocidad_raw) if velocidad_raw is not None else None
    rango_cierre_min = float(rango_cierre_min_raw) if rango_cierre_min_raw is not None else None
    rango_cierre_max = float(rango_cierre_max_raw) if rango_cierre_max_raw is not None else None
    tasacion_min = float(tasacion_min_raw) if tasacion_min_raw not in (None, "", 0, "0") else None
    tasacion_max = float(tasacion_max_raw) if tasacion_max_raw not in (None, "", 0, "0") else None
    campana_recomendada = str(prop.get("campana_recomendada") or "").strip().lower()
    estado_precio_tasacion = str(prop.get("estado_precio_tasacion") or "").strip().lower()
    prioridad_ajuste_tasacion = str(prop.get("prioridad_ajuste_tasacion") or "").strip().lower()
    argumento_comercial = str(prop.get("argumento_comercial") or "").strip()
    riesgo_comercial = str(prop.get("riesgo_comercial") or "").strip()
    nivel_competencia = str(prop.get("nivel_competencia") or "").strip()
    liquidez = str(prop.get("liquidez") or "").strip()
    presion_baja_precio = str(prop.get("presion_baja_precio") or "").strip()
    score_presion = prop.get("score_presion_comercial")
    brecha_pub_cierre_ic = prop.get("brecha_publicacion_vs_cierre_pct")

    tasacion_ref = None
    if tasacion_min is not None and tasacion_max is not None:
        tasacion_ref = (tasacion_min + tasacion_max) / 2.0
    elif tasacion_min is not None:
        tasacion_ref = tasacion_min
    elif tasacion_max is not None:
        tasacion_ref = tasacion_max

    brecha_tasacion_pct = ((precio - tasacion_ref) / tasacion_ref * 100.0) if (tasacion_ref and tasacion_ref > 0 and precio > 0) else None
    usar_ruta_tasacion = brecha_tasacion_pct is not None and brecha_tasacion_pct > 0

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

    if tasacion_ref is not None and tasacion_ref > 0 and "0 UF" in argumento_comercial:
        argumento_comercial = ""

    score_bar_width = score_comercial if score_comercial is not None else 0
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
                <p style="margin:0;font-size:18px;font-weight:700;color:#0f172a;">{actividad_idx}/100</p>
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
                <p style="margin:0;font-size:18px;font-weight:700;color:#0f172a;">{left_val}</p>
              </td>
              <td style="width:50%;padding:0 0 10px 8px;vertical-align:top;">
                <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Promedio comuna</p>
                <p style="margin:0;font-size:18px;font-weight:700;color:#0f172a;">{right_val}</p>
              </td>
            </tr>
            """
        resumen_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;margin:0 0 22px 0;">
          <tr>
            <td style="padding:16px;">
              <p style="margin:0 0 12px 0;font-size:18px;line-height:1.3;color:#0f172a;font-weight:700;">Resumen comercial</p>
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
        comp_pub_uf = f"{avg_pub_uf:,.0f} UF" if avg_pub_uf is not None else "No disponible"
        comp_cierre_uf = f"{avg_cierre_uf:,.0f} UF" if avg_cierre_uf is not None else "No disponible"
        comparacion_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #d7e0eb;border-radius:14px;margin:0 0 22px 0;">
          <tr>
            <td style="padding:18px;">
              <p style="margin:0 0 10px 0;font-size:18px;line-height:1.3;color:#0f172a;font-weight:700;">Comparacion mercado {comuna}</p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  <td style="width:50%;padding:0 8px 0 0;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Promedio publicacion</p>
                    <p style="margin:0;font-size:18px;font-weight:700;color:#0f172a;">{comp_pub_uf}</p>
                  </td>
                  <td style="width:50%;padding:0 0 0 8px;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Promedio cierre</p>
                    <p style="margin:0;font-size:18px;font-weight:700;color:#0f172a;">{comp_cierre_uf}</p>
                  </td>
                </tr>
                <tr>
                  <td style="width:50%;padding:12px 8px 0 0;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Tiempo promedio venta</p>
                    <p style="margin:0;font-size:18px;font-weight:700;color:#0f172a;">{comp_tiempo_pub}</p>
                  </td>
                  <td style="width:50%;padding:12px 0 0 8px;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Tu propiedad</p>
                    <p style="margin:0;font-size:18px;font-weight:700;color:#0f172a;">{comp_tu_prop}</p>
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
            brecha_texto = f"En propiedades similares de {comuna}, el promedio de cierre reciente se ubica {abs(delta_pub_cierre):.1f}% {'bajo' if delta_pub_cierre < 0 else 'sobre'} la publicacion inicial."
        actividad_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #d7e0eb;border-radius:14px;margin:0 0 22px 0;">
          <tr>
            <td style="padding:18px;">
              <p style="margin:0 0 10px 0;font-size:18px;line-height:1.3;color:#0f172a;font-weight:700;">Actividad de mercado</p>
              <p style="margin:0 0 10px 0;font-size:14px;line-height:1.6;color:#475569;">Propiedades similares en {comuna} muestran mejor desempeno comercial en rangos entre <b>{rango_min:,.0f} UF</b> y <b>{rango_max:,.0f} UF</b>.</p>
              <p style="margin:0 0 10px 0;font-size:14px;line-height:1.6;color:#475569;">{brecha_texto}</p>
              <p style="margin:0 0 6px 0;font-size:14px;color:#64748b;">Rango competitivo zona</p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 8px 0;">
                <tr>
                  <td style="font-size:14px;color:#334155;padding:0 0 6px 0;">
                    {rango_min:,.0f} UF
                    <span style="display:inline-block;width:8px;"></span>
                    <span style="display:inline-block;width:56%;height:6px;background:#dbe4ef;border-radius:999px;vertical-align:middle;position:relative;">
                      <span style="display:inline-block;width:10px;height:10px;background:#0f172a;border-radius:50%;position:relative;left:{f'{marker_pct}%' if marker_pct is not None else '100%'};top:-2px;"></span>
                    </span>
                    <span style="display:inline-block;width:8px;"></span>
                    {rango_max:,.0f} UF
                  </td>
                </tr>
              </table>
              <p style="margin:0 0 10px 0;font-size:14px;color:#334155;">Tu publicacion actual: <b>{precio:,.0f} UF</b></p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  <td style="width:50%;padding:0 8px 0 0;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Stock activo similares</p>
                    <p style="margin:0;font-size:16px;color:#0f172a;font-weight:700;">{stock_activo if stock_activo is not None else "No disponible"}</p>
                  </td>
                  <td style="width:50%;padding:0 0 0 8px;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Promedio UF/m2</p>
                    <p style="margin:0;font-size:16px;color:#0f172a;font-weight:700;">{f"{promedio_uf_m2:,.2f} UF/m2" if promedio_uf_m2 is not None else "No disponible"}</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
        """

    score_html = ""
    has_componentes_score = any(v is not None for v in [visibilidad_idx, actividad_idx, competitividad_idx])
    if score_comercial is not None or has_componentes_score:
        score_nivel = (
            "Competitividad alta" if score_comercial is not None and score_comercial >= 75 else
            "Competitividad media" if score_comercial is not None and score_comercial >= 50 else
            "Competitividad baja" if score_comercial is not None else "Indice en evaluacion"
        )
        score_head = f"{score_comercial}/100 — {score_nivel}" if score_comercial is not None else "Indice en evaluacion"
        detalle_score = ""
        if has_componentes_score:
            detalle_score = f"""
              <p style="margin:0 0 6px 0;font-size:14px;color:#e2e8f0;">Visibilidad: <b>{visibilidad_label}</b></p>
              <p style="margin:0 0 6px 0;font-size:14px;color:#e2e8f0;">Interes compradores: <b>{interes_label}</b></p>
              <p style="margin:0;font-size:14px;color:#e2e8f0;">Competitividad precio: <b>{competitividad_label}</b></p>
            """
        else:
            detalle_score = '<p style="margin:0;font-size:14px;color:#e2e8f0;">Detalle de dimensiones no disponible para esta propiedad.</p>'
        score_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#0f172a;border:1px solid #0f172a;border-radius:14px;margin:0 0 22px 0;">
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

    def btn(url: str, text: str, color: str, border: str = "none", text_color: str = "#ffffff") -> str:
        return (
            f'<a href="{url}" style="display:block;padding:14px 20px;background:{color};'
            f'color:{text_color};text-decoration:none;border-radius:999px;font-weight:700;text-align:center;'
            f'border:{border};font-size:16px;line-height:1.2;">{text}</a>'
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
    bloquea_precio_por_campana = campana_recomendada in {"revisar_datos", "revisar_publicacion", "destacar_propiedad"}
    es_recomendacion_precio = (
        accion in {"bajar_precio_urgente", "bajar_precio_sugerida"}
        and nuevo > 0
        and not bloquea_precio_por_tasacion
        and not bloquea_precio_por_campana
    )
    recomendaciones_html = ""
    if usar_ruta_tasacion:
        precio_sugerido_tasacion = nuevo if nuevo > 0 else (round(tasacion_ref) if tasacion_ref is not None else 0)
        variacion_tasacion = (
            f"{((precio_sugerido_tasacion - precio) / precio * 100.0):+.1f}%"
            if (precio > 0 and precio_sugerido_tasacion > 0)
            else "No aplica"
        )
        recomendaciones_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #cbd5e1;border-radius:14px;margin:0 0 26px 0;">
          <tr>
            <td style="padding:18px;">
              <p style="margin:0 0 8px 0;font-size:18px;line-height:1.3;color:#0f172a;font-weight:700;">Evaluacion comercial con respaldo de tasacion y comparables</p>
              <p style="margin:0 0 16px 0;font-size:14px;line-height:1.6;color:#64748b;">La evaluacion comercial observa diferencias entre el posicionamiento actual y el rango competitivo detectado en comparables vendidos y operaciones inscritas recientemente en el Conservador de Bienes Raices.</p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  <td style="padding:0 8px 10px 0;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Precio actual</p>
                    <p style="margin:0;font-size:20px;color:#0f172a;font-weight:700;">{precio:,.0f} UF</p>
                  </td>
                  <td style="padding:0 0 10px 8px;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Posicionamiento competitivo observado</p>
                    <p style="margin:0;font-size:20px;color:#0f172a;font-weight:700;">{precio_sugerido_tasacion:,.0f} UF</p>
                  </td>
                </tr>
              </table>
              <p style="margin:0 0 2px 0;font-size:14px;line-height:1.5;color:#334155;">Tasacion comercial observada: <span style="font-weight:700;color:#0f172a;">{f"{tasacion_min:,.0f} - {tasacion_max:,.0f} UF" if tasacion_min is not None and tasacion_max is not None else "No disponible"}</span></p>
              <p style="margin:2px 0 0 0;font-size:14px;line-height:1.5;color:#334155;">Diferencia frente a referencia de tasacion: <span style="font-weight:700;color:#0f172a;">{brecha_tasacion_pct:+.1f}%</span></p>
              <p style="margin:2px 0 0 0;font-size:14px;line-height:1.5;color:#334155;">Ajuste estimado de posicionamiento: <span style="font-weight:700;color:#0f172a;">{variacion_tasacion}</span></p>
              <p style="margin:8px 0 0 0;font-size:14px;line-height:1.5;color:#334155;">{impacto}</p>
              <p style="margin:10px 0 0 0;font-size:14px;line-height:1.6;color:#334155;">El objetivo de esta evaluacion es ayudarte a mantener una posicion competitiva dentro del comportamiento actual del mercado.</p>
            </td>
          </tr>
        </table>
        """
    elif es_recomendacion_precio:
        recomendaciones_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #cbd5e1;border-radius:14px;margin:0 0 26px 0;">
          <tr>
            <td style="padding:18px;">
              <p style="margin:0 0 8px 0;font-size:18px;line-height:1.3;color:#0f172a;font-weight:700;">{asunto_tag}</p>
              <p style="margin:0 0 18px 0;font-size:14px;line-height:1.6;color:#64748b;">En este caso, la recomendacion prioriza evidencia de comportamiento comercial, comparables activos y ritmo de demanda por comuna.</p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  <td style="padding:0 8px 10px 0;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Precio actual</p>
                    <p style="margin:0;font-size:20px;color:#0f172a;font-weight:700;">{precio:,.0f} UF</p>
                  </td>
                  <td style="padding:0 0 10px 8px;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Precio sugerido</p>
                    <p style="margin:0;font-size:20px;color:#0f172a;font-weight:700;">{nuevo:,.0f} UF</p>
                  </td>
                </tr>
              </table>
              <p style="margin:2px 0 0 0;font-size:14px;line-height:1.5;color:#334155;">Variacion estimada: <span style="font-weight:700;color:#0f172a;">{variacion}</span></p>
              <p style="margin:10px 0 0 0;font-size:14px;line-height:1.6;color:#334155;"><b>Oportunidad esperada:</b><br />• Mayor visibilidad<br />• Incremento de consultas<br />• Mejor posicionamiento competitivo</p>
            </td>
          </tr>
        </table>
        """
    else:
        score_presion_txt = f"{float(score_presion):.0f}/100" if score_presion is not None else "No disponible"
        brecha_ic_txt = f"{float(brecha_pub_cierre_ic):+.1f}%" if brecha_pub_cierre_ic is not None else "No disponible"
        recomendaciones_html = f"""
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;border:1px solid #cbd5e1;border-radius:14px;margin:0 0 26px 0;">
          <tr>
            <td style="padding:18px;">
              <p style="margin:0 0 8px 0;font-size:18px;line-height:1.3;color:#0f172a;font-weight:700;">Recomendacion comercial por datos de mercado</p>
              <p style="margin:0 0 14px 0;font-size:14px;line-height:1.6;color:#64748b;">Este caso no activa ajuste de precio directo. La recomendacion prioriza optimizacion comercial de publicacion y posicionamiento competitivo.</p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  <td style="width:50%;padding:0 8px 8px 0;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Liquidez</p>
                    <p style="margin:0;font-size:16px;color:#0f172a;font-weight:700;">{liquidez or "No disponible"}</p>
                  </td>
                  <td style="width:50%;padding:0 0 8px 8px;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Nivel competencia</p>
                    <p style="margin:0;font-size:16px;color:#0f172a;font-weight:700;">{nivel_competencia or "No disponible"}</p>
                  </td>
                </tr>
                <tr>
                  <td style="width:50%;padding:8px 8px 0 0;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Presion comercial</p>
                    <p style="margin:0;font-size:16px;color:#0f172a;font-weight:700;">{score_presion_txt}</p>
                  </td>
                  <td style="width:50%;padding:8px 0 0 8px;vertical-align:top;">
                    <p style="margin:0 0 4px 0;font-size:14px;color:#64748b;">Brecha publicacion/cierre</p>
                    <p style="margin:0;font-size:16px;color:#0f172a;font-weight:700;">{brecha_ic_txt}</p>
                  </td>
                </tr>
              </table>
              <p style="margin:12px 0 0 0;font-size:14px;line-height:1.6;color:#334155;">{argumento_comercial or "Sugerimos reforzar propuesta comercial, contenido de publicacion y seguimiento activo de demanda por comuna."}</p>
            </td>
          </tr>
        </table>
        """

    bloque_tasacion_adjunta = ""
    if incluye_tasacion_adjunta:
        bloque_tasacion_adjunta = """
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;margin:0 0 22px 0;">
          <tr>
            <td style="padding:18px;">
              <p style="margin:0 0 8px 0;font-size:18px;line-height:1.3;color:#0f172a;font-weight:700;">Informe comercial complementario</p>
                          <p style="margin:0;font-size:14px;line-height:1.65;color:#475569;">Adjuntamos una tasacion comercial desarrollada por Propiteq, plataforma especializada en analisis de mercado inmobiliario. El informe considera comparables vendidos, publicaciones activas y operaciones inscritas recientemente en el Conservador de Bienes Raices, como respaldo tecnico para apoyar la evaluacion comercial de la propiedad.</p>
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
        <title>Recomendacion comercial</title>
      </head>
      <body style="margin:0;padding:0;background:#f3f5f8;font-family:Arial,'Helvetica Neue',Helvetica,sans-serif;color:#1f2937;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:24px 12px;">
          <tr>
            <td align="center">
              <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="max-width:640px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0;">
                <tr>
                  <td style="padding:16px 24px;text-align:center;border-bottom:1px solid #e7edf5;">
                    <img src="{logo_url}" alt="Procasa" style="height:82px;max-width:300px;" />
                  </td>
                </tr>

                <tr>
                  <td style="padding:34px 22px 28px 22px;">
                    <p style="margin:0 0 10px 0;font-size:14px;letter-spacing:0.06em;color:#64748b;text-transform:uppercase;">Informe comercial personalizado</p>
                    <h1 style="margin:0 0 12px 0;font-size:28px;line-height:1.25;color:#0f172a;font-weight:700;">{"Detectamos una oportunidad de alineamiento comercial respecto al mercado actual" if usar_ruta_tasacion else "Detectamos una oportunidad para mejorar el desempeno comercial de tu propiedad"}</h1>
                    <p style="margin:0 0 12px 0;font-size:16px;line-height:1.65;color:#475569;">{"Este analisis se apoya en tasaciones, comparables vendidos y comportamiento de mercado reciente." if usar_ruta_tasacion else "Este analisis considera comportamiento reciente del mercado, actividad de compradores y publicaciones similares en tu comuna."}</p>
                    <p style="margin:0 0 26px 0;font-size:14px;line-height:1.6;color:#475569;">Base de analisis: {origen_analisis}. Cuando existen datos de cierre, se incorporan operaciones inscritas recientemente para estimar posicionamiento competitivo real.</p>

                    {resumen_html}

                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;margin:0 0 22px 0;">
                      <tr>
                        <td style="padding:18px;">
                          <p style="margin:0 0 10px 0;font-size:18px;line-height:1.3;color:#0f172a;font-weight:700;">Propiedad en seguimiento</p>
                          <p style="margin:0 0 6px 0;font-size:14px;color:#64748b;">Codigo</p>
                          <p style="margin:0 0 12px 0;font-size:16px;color:#1e293b;font-weight:700;"><a href="{property_url}" style="color:#0f172a;text-decoration:underline;">{codigo}</a></p>
                          <p style="margin:0 0 6px 0;font-size:14px;color:#64748b;">Tipo</p>
                          <p style="margin:0 0 12px 0;font-size:16px;color:#1e293b;">{tipo}</p>
                          <p style="margin:0 0 6px 0;font-size:14px;color:#64748b;">Comuna</p>
                          <p style="margin:0;font-size:16px;color:#1e293b;">{comuna}</p>
                        </td>
                      </tr>
                    </table>

                    {comparacion_html}

                    {actividad_html}

                    {score_html}

                    {recomendaciones_html}

                    {bloque_tasacion_adjunta}

                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;margin:0 0 24px 0;">
                      <tr>
                        <td style="padding:18px;">
                          <p style="margin:0 0 8px 0;font-size:18px;line-height:1.3;color:#0f172a;font-weight:700;">Observacion comercial</p>
                          <p style="margin:0;font-size:14px;line-height:1.65;color:#475569;">{observacion_comercial}</p>
                        </td>
                      </tr>
                    </table>

                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 12px 0;">
                      <tr>
                        <td>{btn(urls['aceptar_rebaja'], 'Revisar posicionamiento sugerido', '#0f172a')}</td>
                      </tr>
                    </table>

                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 28px 0;">
                      <tr>
                        <td>{btn(urls['contactar_ejecutivo'], 'Evaluar recomendacion con asesor', '#ffffff', '1px solid #94a3b8', '#0f172a')}</td>
                      </tr>
                    </table>

                    <p style="margin:0 0 8px 0;text-align:center;font-size:14px;line-height:1.5;color:#64748b;">
                      <a href="{urls['mantener_precio']}" style="color:#64748b;text-decoration:underline;">Mantener precio actual</a>
                      &nbsp;&nbsp;|&nbsp;&nbsp;
                      <a href="{urls['no_disponible']}" style="color:#64748b;text-decoration:underline;">Propiedad no disponible</a>
                    </p>
                    <p style="margin:0;text-align:center;font-size:12px;line-height:1.5;color:#94a3b8;">
                      <a href="{urls['unsubscribe']}" style="color:#94a3b8;text-decoration:underline;">Dejar de recibir comunicaciones</a>
                    </p>
                  </td>
                </tr>

                <tr>
                  <td style="background:#0f172a;padding:26px 22px;text-align:center;color:#e2e8f0;">
                    <p style="margin:0 0 6px 0;font-size:20px;color:#ffffff;font-weight:700;">{asesor}</p>
                    <p style="margin:0 0 14px 0;font-size:14px;color:#cbd5e1;">Asesora Comercial Inmobiliaria</p>
                    <a href="{whatsapp_url}" style="display:inline-block;background:#22c55e;color:#ffffff;text-decoration:none;padding:12px 20px;border-radius:999px;font-weight:700;font-size:16px;line-height:1.2;">Contacto directo WhatsApp</a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """


def attach_pdf(msg: MIMEMultipart, codigo: str, enabled: bool = True) -> bool:
    if not enabled:
        return False
    pdf = TASACIONES_DIR / f"{codigo}.pdf"
    if not pdf.exists():
        return False
    with pdf.open("rb") as f:
        part = MIMEApplication(f.read(), _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=pdf.name)
        msg.attach(part)
    return True


def send_email(to_email: str, subject: str, html: str, codigo: str, attach_pdf_enabled: bool = True) -> tuple[bool, bool, str]:
    msg = MIMEMultipart()
    msg["From"] = f"Procasa <{Config.GMAIL_USER}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html", "utf-8"))
    attached = attach_pdf(msg, codigo, enabled=attach_pdf_enabled)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(Config.GMAIL_USER, Config.GMAIL_PASSWORD)
        server.sendmail(Config.GMAIL_USER, [to_email], msg.as_string())
    return True, attached, "ok"


def load_wave1(oficina: str) -> list[dict]:
    db = get_db()
    props = list(
        db["propiedades_accionables"].find(
            {
                "oficina": oficina,
                "accion_recomendada": {"$in": ["bajar_precio_urgente", "bajar_precio_sugerida", "revisar_publicacion", "destacar_propiedad"]},
                "ready_para_campana": True,
                "precio_publicado_uf": {"$gt": 0},
            },
            {
                "_id": 0,
                "codigo_propiedad": 1,
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
    )

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
        vmm = tas.get("valor_minimo_maximo") or {}
        ana = t.get("analisis_comercial") or {}

        val_com_uf = vcom.get("uf")
        min_uf = vmm.get("precio_minimo_uf")
        max_uf = vmm.get("precio_maximo_uf")

        min_uf = float(min_uf) if min_uf not in (None, "", 0, "0") else None
        max_uf = float(max_uf) if max_uf not in (None, "", 0, "0") else None
        val_com_uf = float(val_com_uf) if val_com_uf not in (None, "", 0, "0") else None

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

        by_code[c]["estado_precio_tasacion"] = ana.get("estado_precio")
        by_code[c]["prioridad_ajuste_tasacion"] = ana.get("prioridad_ajuste")
        by_code[c]["diferencia_porcentual_tasacion"] = ana.get("diferencia_porcentual")
        if not by_code[c].get("argumento_comercial"):
            by_code[c]["argumento_comercial"] = t.get("argumento_baja_precio")

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


def registrar_envio(db, campana: str, p: dict, to_email: str, ok: bool, attached: bool, token: str, mode: str) -> None:
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
        }
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["test", "live"], default="test")
    ap.add_argument("--test-email", default="pgalleguillos@procasa.cl")
    ap.add_argument("--oficina", default="PROCASA SUCRE")
    ap.add_argument("--campana", default=f"baja_precio_ola1_{datetime.now(timezone.utc).strftime('%Y%m%d')}")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--codigo", default="", help="Codigo de propiedad para pruebas puntuales en modo test")
    args = ap.parse_args()
    test_email_fijo = "pgalleguillos@procasa.cl"

    base_url = Config.CRM_BASE_URL.rstrip("/")
    wave = load_wave1(args.oficina)
    if args.limit > 0:
        wave = wave[: args.limit]

    if not wave:
        print("No hay propiedades para envio")
        return

    if args.mode == "test":
        codigo_input = (args.codigo or "").strip()
        if not codigo_input:
            codigo_input = input("Ingresa codigo_propiedad para prueba: ").strip()
        if not codigo_input:
            print("Debes ingresar un codigo_propiedad en modo test.")
            return
        wave_filtrada = [p for p in wave if str(p.get("codigo_propiedad") or "").strip() == codigo_input]
        if not wave_filtrada:
            print(f"No se encontro la propiedad codigo={codigo_input} en la ola cargada.")
            return
        wave = wave_filtrada

    db = get_db()
    sent = 0
    for i, p in enumerate(wave, start=1):
        codigo = str(p.get("codigo_propiedad") or "")
        email_owner = (p.get("email_propietario") or "").strip()
        target = test_email_fijo if args.mode == "test" else email_owner
        if not target:
            continue

        token = secrets.token_urlsafe(20)
        asesor = (p.get("ejecutivo") or "Equipo Procasa").strip() or "Equipo Procasa"
        email_tracking = email_owner or target
        brecha_tasacion_pct = _calcular_brecha_tasacion_pct(p)
        usa_tasacion = brecha_tasacion_pct is not None and brecha_tasacion_pct > 0
        html = build_html(
            p,
            email_tracking,
            args.campana,
            base_url,
            asesor,
            token,
            mode=args.mode,
            incluye_tasacion_adjunta=usa_tasacion,
        )
        subject = f"Procasa | Recomendacion sobre tu propiedad {codigo}"

        ok = False
        attached = False
        try:
            ok, attached, _ = send_email(target, subject, html, codigo, attach_pdf_enabled=usa_tasacion)
        except Exception as e:
            print(f"[{i}/{len(wave)}] ERROR codigo={codigo} to={target} err={e}")

        registrar_envio(db, args.campana, p, target, ok, attached, token, args.mode)
        if ok:
            sent += 1
            print(f"[{i}/{len(wave)}] enviado codigo={codigo} to={target} pdf_adjunto={attached}")

        if args.mode == "test":
            break

    print(f"Envios completados: {sent}")


if __name__ == "__main__":
    main()
