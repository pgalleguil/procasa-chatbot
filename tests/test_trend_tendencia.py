"""Tests de la Evolución Acumulada de Demanda (Tendencia de Captura).

Cubre: agrupación diaria en America/Santiago (no UTC), borde final exclusivo
del período, y contrato del bucket Chile para leads límite (19:59/20:00/23:59/
00:00). mongomock no implementa el parámetro timezone de $dateToString, por lo
que el comportamiento real se valida por el contrato con zoneinfo y por la
estructura del pipeline.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from analytics.leads_queries import (
    _build_chile_period_bounds,
    query_comparative_trends,
)

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "leads_dashboard.html").read_text(encoding="utf-8")

CHILE = ZoneInfo("America/Santiago")


class _FakeCollection:
    def __init__(self):
        self.pipelines = []

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return []


class _FakeDB:
    def __init__(self):
        self.leads = _FakeCollection()

    def __getitem__(self, key):
        return self.leads


def _chile_date(iso_utc):
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(CHILE).date().isoformat()


def test_trend_pipeline_buckets_por_america_santiago():
    db = _FakeDB()
    import analytics.leads_queries as q
    from unittest.mock import patch
    with patch.object(q, "get_db", return_value=db), \
         patch.object(q, "_normalized_created_at_stage", return_value={}), \
         patch.object(q, "_build_commercial_cohort_match", return_value={}):
        query_comparative_trends("2026-07-16", "2026-08-14",
                                 comparison_start="2026-06-16", comparison_end="2026-07-15")
    assert db.leads.pipelines, "se debe ejecutar la agregación"
    joined = json.dumps(db.leads.pipelines, default=str)
    # El bucket diario debe usar America/Santiago (no UTC).
    assert '"timezone": "America/Santiago"' in joined
    assert "$dateToString" in joined
    # Reconciliación de series: current y previous en el mismo $facet.
    assert '"current"' in joined and '"previous"' in joined


def test_contrato_bucket_chile_bordes():
    # América/Santiago en agosto (DST NO activo, UTC-4). Sin hardcodear -04:00.
    # 20:00 Chile del 05-ago = 00:00 UTC del 06-ago -> bucket 05-ago (Chile).
    assert _chile_date("2026-08-06T00:00:00Z") == "2026-08-05"
    # 23:59 Chile del 05-ago = 03:59 UTC del 06-ago -> bucket 05-ago.
    assert _chile_date("2026-08-06T03:59:59Z") == "2026-08-05"
    # 19:59 Chile del 05-ago = 23:59 UTC del 05-ago -> bucket 05-ago.
    assert _chile_date("2026-08-05T23:59:59Z") == "2026-08-05"
    # 00:00 Chile del 06-ago = 04:00 UTC del 06-ago -> bucket 06-ago.
    assert _chile_date("2026-08-06T04:00:00Z") == "2026-08-06"


def test_bounds_periodo_exclusivo_final():
    # [period_start, period_end): fin = día siguiente 00:00 Chile.
    start_utc, end_utc = _build_chile_period_bounds("2026-07-16", "2026-08-14")
    assert start_utc == datetime(2026, 7, 16, 4, 0, tzinfo=timezone.utc)
    assert end_utc == datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)
    # Un lead exactamente en end_utc queda FUERA del período (límite exclusivo).
    lead_at_end = end_utc
    assert not (start_utc <= lead_at_end < end_utc)
    # Un lead un microsegundo antes SÍ está dentro.
    assert start_utc <= end_utc - datetime.resolution < end_utc


def test_trend_hover_interaction_presente():
    # Interacción hover/tap: snap por área con guía + puntos + tooltip ampliado.
    render = HTML.split("function renderTrendsAndChannels()", 1)[1]
    assert "tc-guide" in render
    assert "tc-hl-cur" in render
    assert "tc-hl-prev" in render
    assert "tc-zone" in render
    assert "Leads del día" in render
    assert "Actual acumulado" in render
    assert "matchMedia('(hover: hover)')" in render
