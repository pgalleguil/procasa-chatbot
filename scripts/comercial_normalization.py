#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Normalizacion centralizada para inteligencia comercial inmobiliaria."""

from __future__ import annotations

import re
import unicodedata
from typing import Tuple

TIPO_MAP = {
    # CASA
    "casa": "Casa",
    "casa habitacion": "Casa",
    "casa_habitacion": "Casa",
    "casa habitación": "Casa",
    # DEPTO
    "departamento": "Departamento",
    "depto": "Departamento",
    "depto.": "Departamento",
    "departamento habitacion": "Departamento",
    "departamento_habitacion": "Departamento",
    # OFICINA
    "oficina": "Oficina",
    "o cina": "Oficina",
    "oficna": "Oficina",
    # GALPON
    "galpon": "Galpón",
    "galpón": "Galpón",
    # BODEGA
    "bodega": "Bodega",
    "bodegas": "Bodega",
    # COMERCIAL
    "comercial": "Comercial",
    "local comercial": "Comercial",
    # AGRICOLA
    "agricola": "Agrícola",
    "agricola forestal": "Agrícola",
    "agrícola forestal": "Agrícola",
    "unidad agroeconomica": "Agrícola",
    "unidad agroeconómica": "Agrícola",
    "unidad_agroeconomica": "Agrícola",
    # PARCELA
    "parcela": "Parcela",
    "parcela agroresidencial": "Parcela",
    "parcela_agroresidencial": "Parcela",
    # ESTACIONAMIENTO
    "estacionamiento": "Estacionamiento",
    "estacionamientos": "Estacionamiento",
    # INDUSTRIAL
    "industrial": "Industrial",
    # TERRENO
    "terreno": "Terreno",
}

COMUNA_FIXES = {
    "via del mar": "viña del mar",
    "valparaso": "valparaíso",
    "concn": "concón",
    "estacin central": "estación central",
    "maip": "maipú",
    "uoa": "ñoñoa",
    "hualpn": "hualpén",
    "chilln": "chillán",
    "curic": "curicó",
    "tom": "tomé",
}


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text or "") if unicodedata.category(c) != "Mn")


def normalize_text_basic(text: str) -> str:
    t = (text or "").replace("\x00", " ").lower().strip()
    t = t.replace("-", " ").replace("_", " ")
    t = re.sub(r"[^a-z0-9áéíóúñ\.\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def normalize_tipo_propiedad(tipo: str) -> str:
    t = normalize_text_basic(tipo)
    t = t.replace("o cina", "oficina")
    t = t.replace("oficna", "oficina")
    t = t.replace("casa habitacion", "casa habitacion")
    t = t.replace("departamento habitacion", "departamento habitacion")
    return TIPO_MAP.get(t, t.title() if t else "Desconocido")


def normalize_comuna(comuna: str) -> str:
    t = normalize_text_basic(comuna)
    t = COMUNA_FIXES.get(t, t)
    return t.title()


def build_match_key(comuna: str, tipo: str) -> str:
    c = strip_accents(normalize_comuna(comuna)).lower().strip()
    t = strip_accents(normalize_tipo_propiedad(tipo)).lower().strip()
    c = re.sub(r"\s+", " ", c)
    t = re.sub(r"\s+", " ", t)
    return f"{c}|{t}"


def normalize_comuna_tipo(comuna: str, tipo: str) -> Tuple[str, str, str]:
    c = normalize_comuna(comuna)
    t = normalize_tipo_propiedad(tipo)
    return c, t, build_match_key(c, t)
