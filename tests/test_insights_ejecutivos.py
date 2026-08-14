"""Tests del motor determinístico de Insights Ejecutivos.

Verifica las reglas principales (SLA, origen, conversión temporal, cobertura),
el límite de 3, el balance prioridad/oportunidad/positivo y que NO use las
claves antiguas de SLA (pct_cumplimiento_sla / mediana_general_min).
"""
import inspect
from pathlib import Path

from analytics.leads_service import build_executive_insights


def _sla(in_sla, vencidos):
    return {"in_sla_pct": in_sla, "open_breached": vencidos, "lead_hot": {}, "lead": {}}


def test_sla_critico_usa_in_sla_pct():
    ins = build_executive_insights(
        demand={"variation_pct": 20.0},
        conversion={"conversion_pct": 5.0, "previous_pct": 4.0},
        sla=_sla(13.1, 171),
        sources={"items": []},
        pipeline={},
    )
    assert ins and ins[0]["tipo"] == "prioridad"
    assert ins[0]["titulo"] == "Respuesta comercial crítica"
    assert "13,1%" in ins[0]["texto"] and "171" in ins[0]["texto"]


def test_origen_dominante_baja_conversion():
    ins = build_executive_insights(
        demand={"variation_pct": 20.0},
        conversion={"conversion_pct": 4.9, "previous_pct": 1.9},
        sla=_sla(13.1, 171),
        sources={"items": [
            {"nombre": "Portal Inmobiliario", "cantidad": 133, "pct": 43.5, "conversion_pct": 1.5},
        ]},
        pipeline={},
    )
    hit = next(i for i in ins if i["titulo"] == "Calidad de la principal fuente")
    assert hit["tipo"] == "prioridad"
    assert "Portal Inmobiliario" in hit["texto"] and "43,5%" in hit["texto"] and "1,5%" in hit["texto"]


def test_origen_baja_conversion_no_fuerza_si_muestra_pequena():
    # n<20 -> no debe activar la regla de origen dominante.
    ins = build_executive_insights(
        demand={"variation_pct": 0.0},
        conversion={"conversion_pct": 4.9, "previous_pct": None},
        sla=_sla(None, 0),
        sources={"items": [
            {"nombre": "X", "cantidad": 10, "pct": 30.0, "conversion_pct": 0.0},
        ]},
        pipeline={},
    )
    assert not any(i["titulo"] == "Calidad de la principal fuente" for i in ins)


def test_senal_favorable_con_lenguaje_prudente():
    ins = build_executive_insights(
        demand={"variation_pct": 0.0},
        conversion={"conversion_pct": 4.9, "previous_pct": None},
        sla=_sla(None, 0),
        sources={"items": [
            {"nombre": "Yapo", "cantidad": 22, "pct": 7.0, "conversion_pct": 13.6},
            {"nombre": "TocToc", "cantidad": 32, "pct": 10.0, "conversion_pct": 12.5},
        ]},
        pipeline={},
    )
    hit = next(i for i in ins if i["tipo"] == "oportunidad")
    assert "volúmenes moderados" in hit["texto"]  # muestra pequeña -> prudente
    assert "es el mejor origen" not in hit["texto"]


def test_volumen_conversion_positivo_solo_si_mejoro():
    # Conversión mejora -> positivo.
    ins = build_executive_insights(
        demand={"variation_pct": 88.9},
        conversion={"conversion_pct": 4.9, "previous_pct": 1.9},
        sla=_sla(None, 0),
        sources={"items": []},
        pipeline={},
    )
    hit = next(i for i in ins if i["tipo"] == "positivo")
    assert "3 pp" in hit["texto"]


def test_volumen_conversion_negativo_cuando_empeora():
    # Conversión empeora (en pp) -> prioridad "divergen".
    ins = build_executive_insights(
        demand={"variation_pct": 40.0},
        conversion={"conversion_pct": 4.0, "previous_pct": 6.0},
        sla=_sla(None, 0),
        sources={"items": []},
        pipeline={},
    )
    hit = next(i for i in ins if i["titulo"] == "Volumen y conversión divergen")
    assert hit["tipo"] == "prioridad"
    assert "2 pp" in hit["texto"]


def test_maximo_3_y_balance_prioridad_oportunidad():
    # 2 prioridades + 1 oportunidad + 1 positivo -> máximo 3, prioridades acotadas.
    ins = build_executive_insights(
        demand={"variation_pct": 88.9},
        conversion={"conversion_pct": 4.9, "previous_pct": 1.9},
        sla=_sla(13.1, 171),
        sources={"items": [
            {"nombre": "Portal Inmobiliario", "cantidad": 133, "pct": 43.5, "conversion_pct": 1.5},
            {"nombre": "Yapo", "cantidad": 22, "pct": 7.0, "conversion_pct": 13.6},
        ]},
        pipeline={},
    )
    assert len(ins) <= 3
    tipos = [i["tipo"] for i in ins]
    assert tipos.count("prioridad") == 2
    assert "oportunidad" in tipos


def test_no_usa_claves_sla_antiguas():
    # Regresión: el motor usa in_sla_pct/open_breached, no pct_cumplimiento_sla
    # ni mediana_general_min.
    src = inspect.getsource(build_executive_insights)
    assert "in_sla_pct" in src and "open_breached" in src
    assert "pct_cumplimiento_sla" not in src
    assert "mediana_general_min" not in src
    # Los textos no usan "el equipo" (copy adaptable a filtros).
    assert "el equipo" not in src.lower()


def test_insight_vacio_si_sin_senales():
    ins = build_executive_insights(
        demand={"variation_pct": None},
        conversion={"conversion_pct": None, "previous_pct": None},
        sla=_sla(None, 0),
        sources={"items": []},
        pipeline={"pct_cartera_con_demanda": 26.9},
    )
    assert ins == []
