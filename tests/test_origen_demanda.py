"""Tests de Origen de Demanda.

Cubre: normalización (variantes fusionadas, Sin información no es Directo),
reconciliación SUM(leads) y SUM(visitas canónicas), Top 6 + Otros y
conversión por origen.
"""
from unittest.mock import patch

import mongomock

from analytics.leads_queries import (
    _normalize_source_name,
    query_leads_dashboard_sources,
)


def _db_with(leads, events=None, visitas=None):
    client = mongomock.MongoClient()
    db = client["URLS"]
    if leads:
        db["leads"].insert_many(leads)
    if events:
        db["crm_events"].insert_many(events)
    if visitas:
        db["visitas"].insert_many(visitas)
    return db


def _run_sources(db):
    import analytics.leads_queries as q
    with patch.object(q, "get_db", return_value=db), \
         patch.object(q, "_normalized_created_at_stage", return_value={}), \
         patch.object(q, "_build_commercial_cohort_match", return_value={}):
        return query_leads_dashboard_sources(
            "2026-07-16", "2026-08-14",
            comparison_start="2026-06-16", comparison_end="2026-07-15")


def test_normalize_sin_informacion_no_es_directo():
    # Ausencia de información no debe presentarse como "Directo".
    assert _normalize_source_name("Sin informacion") == "Sin información"
    assert _normalize_source_name("") == "Sin información"
    assert _normalize_source_name("n/a") == "Sin información"
    # e2e_test se mantiene como tal (no se convierte en Directo).
    assert _normalize_source_name("e2e_test") == "e2e_test"


def test_normalize_variantes_fusionadas():
    assert _normalize_source_name("Portal Inmobiliario") == "Portal Inmobiliario"
    assert _normalize_source_name("PortalInmobiliario") == "Portal Inmobiliario"
    assert _normalize_source_name("TocToc") == "TocToc"
    assert _normalize_source_name("TOCTOC") == "TocToc"
    assert _normalize_source_name("MercadoLibre") == "MercadoLibre"


def test_sources_reconcilia_y_top5_otros():
    # 8 orígenes -> Top 5 + Otros (agregando el resto).
    leads = []
    nombres = ["A", "B", "C", "D", "E", "F", "G", "H"]
    counts = [30, 25, 20, 15, 10, 8, 6, 4]
    for i, (n, c) in enumerate(zip(nombres, counts)):
        for k in range(c):
            leads.append({"_id": f"l{i}_{k}", "created_at": "2026-07-20T12:00:00Z",
                          "prospecto": {"origen": n, "codigo": f"{i}"}})
    db = _db_with(leads)
    res = _run_sources(db)
    assert res["total"] == 118
    items = res["current"]
    assert len(items) == 6
    assert items[-1]["nombre"] == "Otros"
    top_sum = sum(it["cantidad"] for it in items[:-1])
    assert items[-1]["cantidad"] == 118 - top_sum  # agrega el resto (6+4)
    assert sum(it["cantidad"] for it in items) == 118  # reconciliación SUM = total


def test_sources_visitas_canonicas_y_conversion():
    # Evidencia canónica de visita: stage_history + lifecycle + crm_events.
    leads = [
        {"_id": "v1", "created_at": "2026-07-20T12:00:00Z",
         "prospecto": {"origen": "Portal Inmobiliario"},
         "stage_history": [{"to": "VISIT_SCHEDULED", "timestamp": "2026-07-21T12:00:00Z"}]},
        {"_id": "v2", "created_at": "2026-07-20T12:00:00Z",
         "prospecto": {"origen": "MercadoLibre"},
         "lifecycle": {"visit_scheduled_at": "2026-07-22T12:00:00Z"}},
        {"_id": "v3", "created_at": "2026-07-20T12:00:00Z",
         "prospecto": {"origen": "MercadoLibre"}},
        {"_id": "v4", "created_at": "2026-07-20T12:00:00Z",
         "prospecto": {"origen": "Yapo"}},
    ]
    events = [{"lead_id": "v4", "result": "VISITA_AGENDADA", "timestamp": "2026-07-23T12:00:00Z"}]
    db = _db_with(leads, events=events)
    res = _run_sources(db)
    by_name = {it["nombre"]: it for it in res["current"]}
    assert by_name["Portal Inmobiliario"]["visitas"] == 1
    assert by_name["MercadoLibre"]["visitas"] == 1
    assert by_name["Yapo"]["visitas"] == 1
    assert res["total_visitas"] == 3
    # Conversión por origen (visitas/leads).
    assert by_name["Portal Inmobiliario"]["conversion_pct"] == 100.0
    assert by_name["MercadoLibre"]["conversion_pct"] == 50.0
