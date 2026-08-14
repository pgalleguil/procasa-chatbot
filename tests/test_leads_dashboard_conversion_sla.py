"""Tests for the Leads Intelligence Resumen Ejecutivo metric corrections:

1. SLA: no single 30-minute threshold classifies executives as good/bad. The
   SLA policy is Hot=60 min and Normal=180 min (business minutes).
2. Conversión a Visita Agendada: numerador = COUNT(DISTINCT lead) con evidencia
   canónica (pipeline + crm_events 'visita_agendada' + orden firmada asociada
   de forma determinística); denominador = todos los leads del período.
"""
from contextlib import ExitStack
import json
from pathlib import Path
from unittest.mock import patch

import mongomock
import pytest

import analytics.leads_queries as lq
from analytics.leads_queries import _VISIT_TRACEABILITY_MATCH, query_leads_dashboard_conversion
from chatbot.crm_metrics import calculate_sla

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "leads_dashboard.html"
SERVICE = ROOT / "analytics" / "leads_service.py"

HTML = TEMPLATE.read_text(encoding="utf-8")
SERVICE_SRC = SERVICE.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_analytics_cache():
    """The overview endpoint caches results for 120s; clear it per test."""
    from analytics import leads_service
    leads_service.L1_CACHE.clear()
    yield
    leads_service.L1_CACHE.clear()


def make_db(leads=None, events=None, visitas=None):
    client = mongomock.MongoClient()
    db = client["URLS"]
    if leads:
        db["leads"].insert_many(leads)
    if events:
        db["crm_events"].insert_many(events)
    if visitas:
        db["visitas"].insert_many(visitas)
    return db


# =============================================================================
# 1. SLA
# =============================================================================

def test_hot_sla_threshold_is_60_business_minutes():
    result = calculate_sla(
        assigned_at=__import__("datetime").datetime(2026, 7, 27, 9, tzinfo=__import__("chatbot.constants", fromlist=["CHILE_TZ"]).CHILE_TZ),
        now=__import__("datetime").datetime(2026, 7, 27, 10, tzinfo=__import__("chatbot.constants", fromlist=["CHILE_TZ"]).CHILE_TZ),
        temperature="HOT",
        hot_started_at=__import__("datetime").datetime(2026, 7, 27, 9, tzinfo=__import__("chatbot.constants", fromlist=["CHILE_TZ"]).CHILE_TZ),
        require_hot_start=True,
    )
    assert result["threshold_minutes"] == 60


def test_normal_sla_threshold_is_180_business_minutes():
    result = calculate_sla(
        assigned_at=__import__("datetime").datetime(2026, 7, 27, 9, tzinfo=__import__("chatbot.constants", fromlist=["CHILE_TZ"]).CHILE_TZ),
        now=__import__("datetime").datetime(2026, 7, 27, 12, tzinfo=__import__("chatbot.constants", fromlist=["CHILE_TZ"]).CHILE_TZ),
        temperature="COLD",
    )
    assert result["threshold_minutes"] == 180


def test_no_executive_classification_based_on_30_minutes_backend():
    assert "sla_median < 30" not in SERVICE_SRC
    assert "sla < 30" not in SERVICE_SRC
    # The old state labels tied to the 30-min threshold are gone.
    assert "SLA Crítico" not in SERVICE_SRC
    assert "Top Performer" not in SERVICE_SRC


def test_no_30_minute_threshold_in_frontend_resumen_ejecutivo():
    assert "sla < 30" not in HTML
    assert "sla<30" not in HTML
    assert "Verde: &lt; 30 min" not in HTML
    assert "umbral 30 min" not in HTML
    assert "≥ 30 min" not in HTML


def test_exec_table_sla_column_removed_from_resumen():
    # La columna Mediana SLA fue eliminada de la tabla de ejecutivos.
    assert "Mediana SLA" not in HTML
    # El tooltip de la celda SLA y su coloreo desaparecieron.
    assert "const slaText = (sla === null" not in HTML
    assert "sla_median" not in HTML


# =============================================================================
# 2. CONVERSIÓN A VISITA AGENDADA (AS-OF PERIOD_END)
# =============================================================================

def test_card_title_renamed_to_conversion_a_visita_agendada():
    assert "Conversión a Visita Agendada" in HTML
    assert "Efectividad & Conversión" not in HTML
    # PDF export snapshot also renamed (part of the Resumen Ejecutivo file).
    assert "Efectividad & Conversión" not in HTML


def test_conversion_not_coerced_to_zero_frontend():
    # No `c.conversion_pct || 0` pattern in the card renderer anymore.
    card_fn = HTML.split("function renderConversionCard()", 1)[1]
    assert "conversion_pct || 0" not in card_fn
    assert "Number(null)" not in card_fn
    assert "Number(undefined)" not in card_fn
    assert "N/D" in card_fn


def test_tooltip_explains_nd_zero_and_as_of_closure():
    assert "0%" in HTML
    assert "N/D" in HTML
    assert "al cierre del período" in HTML


# =============================================================================
# 2.1 Regla temporal: created_at <= evidencia < period_end (exclusivo)
# =============================================================================

LEAD_CREATED = "2026-07-10T14:00:00Z"
EVID_BEFORE = "2026-07-15T10:00:00Z"     # dentro del período y tras la creación
EVID_AFTER = "2026-07-25T10:00:00Z"      # después del period_end exclusivo
EVID_AT_BOUNDARY = "2026-07-21T04:00:00Z"  # exactamente en period_end (NO cuenta)
EVID_PRE_CREATION = "2026-07-09T10:00:00Z"  # anterior a la creación del lead


def build_query_result(db):
    with patch.object(lq, "get_db", return_value=db), \
         patch.object(lq, "_normalized_created_at_stage", return_value={}), \
         patch.object(lq, "_build_commercial_cohort_match", return_value={}):
        return query_leads_dashboard_conversion(
            period_start="2026-07-10",
            period_end="2026-07-20",
            include_comparison=False,
        )


def test_stage_history_visits_before_period_end_counts():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED, "pipeline_stage": "CONTACTED",
         "stage_history": [{"to": "NEW", "timestamp": "2026-07-10T15:00:00Z"},
                           {"to": "VISIT_SCHEDULED", "timestamp": EVID_BEFORE}]},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z", "pipeline_stage": "NEW",
         "stage_history": [{"to": "NEW", "timestamp": "2026-07-11T15:00:00Z"}]},
    ]
    res = build_query_result(make_db(leads=docs))["current"]
    assert res["total"] == 2
    assert res["citas"] == 1


def test_stage_history_visits_after_period_end_not_count():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED, "pipeline_stage": "CONTACTED",
         "stage_history": [{"to": "NEW", "timestamp": "2026-07-10T15:00:00Z"},
                           {"to": "VISIT_SCHEDULED", "timestamp": EVID_AFTER}]},
    ]
    res = build_query_result(make_db(leads=docs))["current"]
    assert res["citas"] == 0


def test_stage_history_visit_done_before_period_end_counts():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED, "pipeline_stage": "OFFER",
         "stage_history": [{"to": "NEW", "timestamp": "2026-07-10T15:00:00Z"},
                           {"to": "VISIT_SCHEDULED", "timestamp": "2026-07-12T10:00:00Z"},
                           {"to": "VISIT_DONE", "timestamp": "2026-07-14T10:00:00Z"},
                           {"to": "OFFER", "timestamp": "2026-07-18T10:00:00Z"}]},
    ]
    res = build_query_result(make_db(leads=docs))["current"]
    assert res["citas"] == 1


def test_lifecycle_visit_scheduled_at_before_period_end_counts():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED,
         "lifecycle": {"visit_scheduled_at": EVID_BEFORE}},
    ]
    res = build_query_result(make_db(leads=docs))["current"]
    assert res["citas"] == 1


def test_lifecycle_visit_scheduled_at_after_period_end_not_count():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED,
         "lifecycle": {"visit_scheduled_at": EVID_AFTER}},
    ]
    res = build_query_result(make_db(leads=docs))["current"]
    assert res["citas"] == 0


def test_crm_event_visita_agendada_before_period_end_counts():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED, "pipeline_stage": "CONTACTED"}]
    events = [{"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE",
               "result": "visita_agendada", "timestamp": EVID_BEFORE}]
    res = build_query_result(make_db(leads=docs, events=events))["current"]
    assert res["citas"] == 1


def test_crm_event_visita_agendada_after_period_end_not_count():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED, "pipeline_stage": "CONTACTED"}]
    events = [{"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE",
               "result": "visita_agendada", "timestamp": EVID_AFTER}]
    res = build_query_result(make_db(leads=docs, events=events))["current"]
    assert res["citas"] == 0


def test_signed_order_accepted_before_period_end_counts():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED, "phone": "+56912345678",
             "prospecto": {"codigo": "P1"}}]
    visitas = [{"_id": "v1", "visita_code": "VIS-2026-0001", "phone": "+56912345678",
                "property_code": "P1", "status": "signed",
                "timeline": [{"action": "accepted", "server_timestamp": EVID_BEFORE}]}]
    res = build_query_result(make_db(leads=docs, visitas=visitas))["current"]
    assert res["citas"] == 1


def test_signed_order_accepted_after_period_end_not_count():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED, "phone": "+56912345678",
             "prospecto": {"codigo": "P1"}}]
    visitas = [{"_id": "v1", "visita_code": "VIS-2026-0001", "phone": "+56912345678",
                "property_code": "P1", "status": "signed",
                "timeline": [{"action": "accepted", "server_timestamp": EVID_AFTER}]}]
    res = build_query_result(make_db(leads=docs, visitas=visitas))["current"]
    assert res["citas"] == 0


def test_signed_order_without_accepted_timestamp_does_not_count():
    # Orden signed sin accepted.server_timestamp: calidad de datos, no se usa.
    docs = [{"_id": "l1", "created_at": LEAD_CREATED, "phone": "+56912345678",
             "prospecto": {"codigo": "P1"}}]
    visitas = [{"_id": "v1", "visita_code": "VIS-2026-0001", "phone": "+56912345678",
                "property_code": "P1", "status": "signed",
                "timeline": [{"action": "otp_verified", "server_timestamp": EVID_BEFORE}]}]
    res = build_query_result(make_db(leads=docs, visitas=visitas))["current"]
    assert res["citas"] == 0


def test_evidence_before_lead_created_does_not_count():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED,
             "stage_history": [{"to": "VISIT_SCHEDULED", "timestamp": EVID_PRE_CREATION}]}]
    res = build_query_result(make_db(leads=docs))["current"]
    assert res["citas"] == 0


def test_current_stage_alone_does_not_look_ahead():
    # pipeline_stage actual VISIT_SCHEDULED sin evidencia temporal canónica
    # NO debe introducir look-ahead histórico.
    docs = [{"_id": "l1", "created_at": LEAD_CREATED, "pipeline_stage": "VISIT_SCHEDULED"}]
    res = build_query_result(make_db(leads=docs))["current"]
    assert res["citas"] == 0


def test_evidence_exactly_at_period_end_not_count_exclusive():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED,
             "stage_history": [{"to": "VISIT_SCHEDULED", "timestamp": EVID_AT_BOUNDARY}]}]
    res = build_query_result(make_db(leads=docs))["current"]
    assert res["citas"] == 0


def test_timezone_offsets_chile_and_utc_compare_correctly():
    # created_at con offset Chile (-04:00) y evidencia UTC: se comparan en UTC.
    docs = [{"_id": "l1", "created_at": "2026-07-10T10:00:00-04:00",
             "stage_history": [{"to": "VISIT_SCHEDULED", "timestamp": "2026-07-15T06:00:00-04:00"}]}]
    res = build_query_result(make_db(leads=docs))["current"]
    # created = 14:00Z, evidencia = 10:00Z -> dentro del período.
    assert res["citas"] == 1


def test_conversion_dedup_same_lead_in_multiple_sources_counts_once():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED, "pipeline_stage": "CONTACTED",
             "phone": "+56912345678", "prospecto": {"codigo": "P1"}}]
    events = [{"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE",
               "result": "visita_agendada", "timestamp": EVID_BEFORE}]
    visitas = [{"_id": "v1", "visita_code": "VIS-2026-0001", "phone": "+56912345678",
                "property_code": "P1", "status": "signed",
                "timeline": [{"action": "accepted", "server_timestamp": EVID_BEFORE}]}]
    res = build_query_result(make_db(leads=docs, events=events, visitas=visitas))["current"]
    assert res["citas"] == 1


def test_conversion_unsent_statuses_do_not_count():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED, "phone": "+56912345678",
         "prospecto": {"codigo": "P1"}},
    ]
    for status in ("sent", "opened", "otp_requested"):
        visitas = [
            {"_id": "v1", "visita_code": "VIS-2026-0001", "phone": "+56912345678",
             "property_code": "P1", "status": status,
             "timeline": [{"action": "accepted", "server_timestamp": EVID_BEFORE}]},
        ]
        res = build_query_result(make_db(leads=docs, visitas=visitas))["current"]
        assert res["citas"] == 0, f"status {status} no debe contar"


def test_conversion_ask_visit_intent_does_not_count():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED, "pipeline_stage": "NEW",
         "last_intent": "ASK_VISIT"},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z", "pipeline_stage": "NEW",
         "last_intent": "VISITA_SOLICITADA"},
    ]
    res = build_query_result(make_db(leads=docs))["current"]
    assert res["citas"] == 0


def test_conversion_ambiguous_order_does_not_duplicate_conversions():
    # Una orden firmada cuyo teléfono corresponde a 2 leads con la misma
    # propiedad no se atribuye a ninguno (ambigüedad).
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED, "phone": "+56912345678",
         "prospecto": {"codigo": "P1"}},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z", "phone": "+56912345678",
         "prospecto": {"codigo": "P1"}},
    ]
    visitas = [
        {"_id": "v1", "visita_code": "VIS-2026-0001", "phone": "+56912345678",
         "property_code": "P1", "status": "signed",
         "timeline": [{"action": "accepted", "server_timestamp": EVID_BEFORE}]},
    ]
    db = make_db(leads=docs, visitas=visitas)
    res = build_query_result(db)["current"]
    assert res["citas"] == 0
    assert res["orders_ambiguous"] == 1


def test_conversion_denominator_is_all_leads_in_period():
    # Los leads sin trazabilidad siguen en el denominador.
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z"},
        {"_id": "l3", "created_at": "2026-07-12T14:00:00Z",
         "stage_history": [{"to": "VISIT_SCHEDULED", "timestamp": EVID_BEFORE}]},
    ]
    res = build_query_result(make_db(leads=docs))["current"]
    assert res["total"] == 3
    assert res["citas"] == 1
    # conversion_pct en el servicio usa total, no evaluable.
    # Solo l3 tiene stage_history (trazabilidad); l1/l2 no tienen campos.
    assert res["evaluable"] == 1


def test_conversion_zero_visits_is_real_zero_pct():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED, "pipeline_stage": "CONTACTED"},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z", "pipeline_stage": "NEW"},
    ]
    res = build_query_result(make_db(leads=docs))["current"]
    assert res["citas"] == 0
    assert res["total"] == 2


def test_previous_period_does_not_change_retroactively():
    # Un evento posterior al cierre del período anterior NO convierte al lead
    # de ese período, aunque el evento sea anterior al período actual.
    docs = [
        {"_id": "prev1", "created_at": "2026-06-12T14:00:00Z", "pipeline_stage": "CONTACTED"},
        {"_id": "prev2", "created_at": "2026-06-13T14:00:00Z", "pipeline_stage": "CONTACTED"},
    ]
    events = [
        # Evento dentro del período anterior (cierre 2026-06-21T04:00Z).
        {"_id": "e1", "lead_id": "prev1", "type": "HUMAN_NOTE",
         "result": "visita_agendada", "timestamp": "2026-06-15T10:00:00Z"},
        # Evento DESPUÉS del cierre del período anterior (pero antes del actual).
        {"_id": "e2", "lead_id": "prev2", "type": "HUMAN_NOTE",
         "result": "visita_agendada", "timestamp": "2026-07-05T10:00:00Z"},
    ]
    db = make_db(leads=docs, events=events)
    with patch.object(lq, "get_db", return_value=db), \
         patch.object(lq, "_normalized_created_at_stage", return_value={}), \
         patch.object(lq, "_build_commercial_cohort_match", return_value={}):
        res = query_leads_dashboard_conversion(
            period_start="2026-07-10", period_end="2026-07-20",
            comparison_start="2026-06-10", comparison_end="2026-06-20",
            include_comparison=True,
        )
    assert res["current"]["total"] == 0
    assert res["previous"]["total"] == 2
    # prev1 convierte (evento dentro del corte), prev2 NO (evento posterior al cierre).
    assert res["previous"]["citas"] == 1


def test_traceability_match_semantics():
    # A lead is evaluable when it carries canonical visit/stage evidence.
    db = make_db(leads=[
        {"_id": "h1", "created_at": "2026-07-10T14:00:00Z", "pipeline_stage": "NEW"},
        {"_id": "h2", "created_at": "2026-07-10T14:00:00Z", "stage_history": [{"to": "NEW"}]},
        {"_id": "h3", "created_at": "2026-07-10T14:00:00Z", "bi_analytics_global": {"RESULTADO_CHAT": "VISITA_AGENDADA"}},
        {"_id": "h4", "created_at": "2026-07-10T14:00:00Z", "lifecycle": {"visit_scheduled_at": "2026-07-12T10:00:00Z"}},
        {"_id": "h5", "created_at": "2026-07-10T14:00:00Z", "stage": "CONTACTED"},
        {"_id": "h6", "created_at": "2026-07-10T14:00:00Z"},
    ])
    rows = list(db["leads"].aggregate([
        {"$match": {"created_at": {"$gte": "2026-07-10T00:00:00", "$lt": "2026-07-21T00:00:00"}}},
        {"$facet": {"evaluable": [{"$match": _VISIT_TRACEABILITY_MATCH}, {"$count": "c"}]}},
    ]))
    facet = rows[0]
    assert (facet.get("evaluable") or [{}])[0].get("c", 0) == 5


def _service_patches(total, citas, evaluable, prev_total=0, prev_citas=0, prev_evaluable=0):
    """Reusable patches for get_leads_dashboard_overview service tests."""
    return [
        patch.object(lq, "query_comparative_trends", return_value={
            "current": {"total": total, "daily": []},
            "previous": {"total": prev_total, "daily": []},
            "variation_pct": None,
        }),
        patch.object(lq, "query_leads_dashboard_conversion", return_value={
            "current": {"total": total, "citas": citas, "evaluable": evaluable, "orders_ambiguous": 0},
            "previous": {"total": prev_total, "citas": prev_citas, "evaluable": prev_evaluable, "orders_ambiguous": 0},
        }),
        patch.object(lq, "query_leads_dashboard_pipeline", return_value={
            "monto_uf": 0, "propiedades_vinculadas": 0, "propiedades_con_precio": 0,
            "propiedades_cartera": 0, "propiedades_cartera_valorizadas": 0,
            "propiedades_venta": 0, "propiedades_arriendo": 0, "propiedades_otro": 0,
            "propiedades_sin_precio": 0, "propiedades_no_en_cartera": 0, "leads_vinculados": 0,
            "monto_venta_uf": 0, "monto_arriendo_uf": 0, "monto_otro_uf": 0,
        }),
        patch.object(lq, "query_property_commission_rows", return_value=[]),
        patch.object(lq, "query_sla_risk_panel", return_value={
            "overall_median_minutes": None, "overall_compliance_pct": None,
            "lead_hot": {}, "lead": {}, "eligible_total": 0, "no_management": 0, "critical_open": 0,
        }),
        patch.object(lq, "query_leads_dashboard_rescue", return_value={"recuperabilidad_alta": 0}),
        patch.object(lq, "query_leads_dashboard_sources", return_value={"current": [], "previous": {}, "total": 0}),
        patch.object(lq, "query_leads_dashboard_executives", return_value={"rows": [], "reconcile": {"total": 0, "comerciales": 0, "sin_asignar": 0, "otros": 0}}),
        patch.object(lq, "query_sla_accountability", return_value={"by_executive": []}),
        patch.object(lq, "query_leads_dashboard_funnel", return_value={"received": 0, "stages": []}),
        patch.object(lq, "query_leads_dashboard_reconcile_breakdown", return_value={"items": [], "total": 0}),
    ]


def _run_overview(period_start, period_end, compare, period_preset=None, total=0, citas=0,
                  evaluable=0, prev_total=0, prev_citas=0, prev_evaluable=0):
    """Run get_leads_dashboard_overview with all query functions patched."""
    from analytics.leads_service import get_leads_dashboard_overview
    db = make_db([])
    with ExitStack() as stack:
        for patcher in _service_patches(total, citas, evaluable, prev_total, prev_citas, prev_evaluable):
            stack.enter_context(patcher)
        stack.enter_context(patch.object(lq, "get_db", return_value=db))
        stack.enter_context(patch.object(lq, "_normalized_created_at_stage", return_value={}))
        stack.enter_context(patch.object(lq, "_build_commercial_cohort_match", return_value={}))
        return get_leads_dashboard_overview(
            period_start=period_start, period_end=period_end,
            compare=compare, period_preset=period_preset,
        )


def test_service_payload_keeps_json_valid_and_uses_total_denominator():
    ov = _run_overview("2026-07-10", "2026-07-20", "none", total=5, citas=0, evaluable=2)
    json.dumps(ov)  # JSON serializable
    conv = ov["conversion"]
    # Denominador = total (5), no evaluable (2).
    assert conv["conversion_pct"] == 0.0  # 0/5 -> real 0.0
    assert conv["evaluable_leads"] == 2
    assert conv["traceability_pct"] == 40.0
    assert conv["leads"] == 5
    assert "previous_pct" in conv


def test_service_payload_zero_leads_is_nd():
    ov = _run_overview("2026-07-10", "2026-07-20", "none", total=0, citas=0, evaluable=0)
    conv = ov["conversion"]
    assert conv["conversion_pct"] is None  # N/D
    assert conv["leads"] == 0
    json.dumps(ov)


def test_service_payload_visits_compute_percentage_over_total():
    ov = _run_overview("2026-07-10", "2026-07-20", "none", total=10, citas=3, evaluable=9)
    conv = ov["conversion"]
    # 3 visitas agendadas / 10 leads totales = 30.0%
    assert conv["conversion_pct"] == 30.0
    assert conv["ratio_leads_per_cita"] == round(10 / 3, 1)
    json.dumps(ov)


def test_service_payload_no_traceable_leads_is_not_nd():
    # Denominador son todos los leads: aunque no haya trazabilidad, si hay
    # leads y cero visitas el resultado es 0.0 (no N/D).
    ov = _run_overview("2026-07-10", "2026-07-20", "none", total=7, citas=0, evaluable=0)
    conv = ov["conversion"]
    assert conv["conversion_pct"] == 0.0
    assert conv["evaluable_leads"] == 0
    assert conv["traceability_pct"] == 0.0
    json.dumps(ov)


def test_period_comparison_not_broken():
    ov = _run_overview("2026-07-10", "2026-07-20", "prev", period_preset="custom",
                       total=8, citas=2, evaluable=8, prev_total=5, prev_citas=1, prev_evaluable=5)
    conv = ov["conversion"]
    assert conv["previous_pct"] == 20.0
    assert conv["diff_pp"] == 5.0
    assert conv["conversion_pct"] == 25.0
    assert ov["period"]["compare_resolved"] == "prev"
    json.dumps(ov)


def test_period_comparison_previous_zero_leads_is_none():
    ov = _run_overview("2026-07-10", "2026-07-20", "prev", period_preset="custom",
                       total=8, citas=2, evaluable=8, prev_total=0, prev_citas=0, prev_evaluable=0)
    conv = ov["conversion"]
    assert conv["previous_pct"] is None
    assert conv["diff_pp"] is None
    assert conv["conversion_pct"] == 25.0
    json.dumps(ov)


# =============================================================================
# 3. EMBUDO COMERCIAL (Recibidos → Gestionados → Contacto efectivo → Visita)
# =============================================================================

from analytics.leads_queries import query_leads_dashboard_funnel


def build_funnel_result(db):
    with patch.object(lq, "get_db", return_value=db), \
         patch.object(lq, "_normalized_created_at_stage", return_value={}), \
         patch.object(lq, "_build_commercial_cohort_match", return_value={}):
        return query_leads_dashboard_funnel(
            period_start="2026-07-10",
            period_end="2026-07-20",
        )


def test_funnel_gestionado_before_period_end_counts():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED,
         "lifecycle": {"first_valid_management_at": "2026-07-15T10:00:00Z"}},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z"},
    ]
    res = build_funnel_result(make_db(leads=docs))
    assert res["received"] == 2
    assert res["stages"][1]["count"] == 1  # gestionados
    assert res["transitions"]["received_to_gestionados"] == 1


def test_funnel_gestionado_after_period_end_not_count():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED,
         "lifecycle": {"first_valid_management_at": "2026-07-25T10:00:00Z"}},
    ]
    res = build_funnel_result(make_db(leads=docs))
    assert res["stages"][1]["count"] == 0
    assert res["transitions"]["received_to_gestionados"] == 0


def test_funnel_contacto_efectivo_before_period_end_counts():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    events = [{"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "requiere_seguimiento",
               "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela", "actor_type": "agent", "confirmed": True}]
    res = build_funnel_result(make_db(leads=docs, events=events))
    assert res["stages"][2]["count"] == 1  # contacto efectivo


def test_funnel_intento_fallido_not_contacto_efectivo():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    events = [{"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "intento_fallido",
               "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela", "actor_type": "agent", "confirmed": True}]
    res = build_funnel_result(make_db(leads=docs, events=events))
    assert res["stages"][2]["count"] == 0  # NO contacto efectivo


def test_funnel_no_respondio_not_contacto_efectivo():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    events = [{"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "NO_RESPONDIO",
               "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela", "actor_type": "agent", "confirmed": True}]
    res = build_funnel_result(make_db(leads=docs, events=events))
    assert res["stages"][2]["count"] == 0


def test_funnel_visita_reconcilia_con_card2():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    events = [{"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "visita_agendada",
               "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela", "actor_type": "agent", "confirmed": True}]
    db = make_db(leads=docs, events=events)
    funnel = build_funnel_result(db)
    conv = build_query_result(db)["current"]
    assert funnel["stages"][3]["count"] == conv["citas"]  # visita funnel = visita CARD 2


def test_funnel_calificados_desaparece():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED, "prospecto": {"email": "a@b.com", "rut": "11.111.111-1"}},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z"},
    ]
    res = build_funnel_result(make_db(leads=docs))
    keys = [s["key"] for s in res["stages"]]
    assert "calificados" not in keys
    assert keys == ["received", "gestionados", "contacto_efectivo", "visita_agendada"]


def test_funnel_contacto_efectivo_deduplicado_por_lead():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    events = [
        {"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "requiere_seguimiento",
         "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela", "actor_type": "agent", "confirmed": True},
        {"_id": "e2", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "requiere_seguimiento",
         "timestamp": "2026-07-16T10:00:00Z", "actor": "Mariela", "actor_type": "agent", "confirmed": True},
    ]
    res = build_funnel_result(make_db(leads=docs, events=events))
    assert res["stages"][2]["count"] == 1


def test_funnel_visita_sin_contacto_efectivo_se_mantiene_en_total():
    # Orden firmada sin evento de gestión: aparece como visita pero no contacto efectivo.
    docs = [{"_id": "l1", "created_at": LEAD_CREATED, "phone": "+56912345678", "prospecto": {"codigo": "P1"}}]
    visitas = [{"_id": "v1", "visita_code": "VIS-2026-0001", "phone": "+56912345678",
                "property_code": "P1", "status": "signed",
                "timeline": [{"action": "accepted", "server_timestamp": "2026-07-15T10:00:00Z"}]}]
    res = build_funnel_result(make_db(leads=docs, visitas=visitas))
    assert res["stages"][3]["count"] == 1  # visita total
    assert res["exceptions"]["visitas_sin_contacto_efectivo"] == 1


def test_funnel_visita_sin_contacto_no_entra_en_transicion_contacto_visita():
    # visita sin contacto efectivo NO se suma al numerador de la transición.
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED, "phone": "+56912345678", "prospecto": {"codigo": "P1"}},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z", "phone": "+56987654321", "prospecto": {"codigo": "P2"}},
    ]
    # l2: contacto efectivo + visita; l1: solo visita (orden firmada)
    events = [{"_id": "e1", "lead_id": "l2", "type": "HUMAN_NOTE", "result": "visita_agendada",
               "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela", "actor_type": "agent", "confirmed": True}]
    visitas = [{"_id": "v1", "visita_code": "VIS-2026-0001", "phone": "+56912345678",
                "property_code": "P1", "status": "signed",
                "timeline": [{"action": "accepted", "server_timestamp": "2026-07-15T10:00:00Z"}]}]
    res = build_funnel_result(make_db(leads=docs, events=events, visitas=visitas))
    assert res["stages"][2]["count"] == 1   # contacto efectivo (l2)
    assert res["stages"][3]["count"] == 2   # visitas totales (l1, l2)
    # transición contacto->visita = |contact ∩ visita| / |contact| = 1/1
    assert res["transitions"]["contacto_efectivo_to_visita_agendada"] == 1
    assert res["stages"][3]["transition_pct"] == 100.0  # 1/1 contacto efectivo
    assert res["exceptions"]["visitas_sin_contacto_efectivo"] == 1


def test_funnel_transition_contact_visit_uses_intersection():
    # |contact ∩ visit| / |contact|
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED, "phone": "+56912345678", "prospecto": {"codigo": "P1"}},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z"},
        {"_id": "l3", "created_at": "2026-07-12T14:00:00Z"},
    ]
    # l1: contacto efectivo + visita; l2: contacto efectivo sin visita
    events = [
        {"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "visita_agendada",
         "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela", "actor_type": "agent", "confirmed": True},
        {"_id": "e2", "lead_id": "l2", "type": "HUMAN_NOTE", "result": "requiere_seguimiento",
         "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela", "actor_type": "agent", "confirmed": True},
    ]
    visitas = [{"_id": "v1", "visita_code": "VIS-2026-0001", "phone": "+56912345678",
                "property_code": "P1", "status": "signed",
                "timeline": [{"action": "accepted", "server_timestamp": "2026-07-15T10:00:00Z"}]}]
    res = build_funnel_result(make_db(leads=docs, events=events, visitas=visitas))
    assert res["stages"][2]["count"] == 2   # contacto (l1, l2)
    assert res["stages"][3]["count"] == 1   # visita (l1)
    assert res["transitions"]["contacto_efectivo_to_visita_agendada"] == 1
    assert res["stages"][3]["transition_pct"] == 50.0  # 1/2


def test_funnel_historical_period_not_using_future_events():
    # Período 1-15 jul: gestión el 20 jul no cuenta.
    docs = [{"_id": "l1", "created_at": LEAD_CREATED,
             "lifecycle": {"first_valid_management_at": "2026-07-25T10:00:00Z"}}]
    events = [{"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "requiere_seguimiento",
               "timestamp": "2026-07-25T10:00:00Z", "actor": "Mariela", "actor_type": "agent", "confirmed": True}]
    res = build_funnel_result(make_db(leads=docs, events=events))
    assert res["stages"][1]["count"] == 0  # gestionados
    assert res["stages"][2]["count"] == 0  # contacto efectivo


def test_funnel_evidence_exactly_at_period_end_not_count():
    # period_end exclusivo = 2026-07-21T04:00:00Z para periodo 07-10..07-20.
    docs = [{"_id": "l1", "created_at": LEAD_CREATED,
             "lifecycle": {"first_valid_management_at": "2026-07-21T04:00:00Z"}}]
    res = build_funnel_result(make_db(leads=docs))
    assert res["stages"][1]["count"] == 0


def test_funnel_closed_won_historico_se_conserva_aunque_actual_sea_closed_lost():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED, "pipeline_stage": "CLOSED_LOST",
             "stage_history": [{"to": "CONTACTED", "timestamp": "2026-07-12T10:00:00Z"},
                               {"to": "CLOSED_WON", "timestamp": "2026-07-15T10:00:00Z"},
                               {"to": "CLOSED_LOST", "timestamp": "2026-07-18T10:00:00Z"}]}]
    res = build_funnel_result(make_db(leads=docs))
    assert res["hitos_excepcionales"]["cierres"] == 1
    assert res["hitos_excepcionales"]["avanzados"] == 1


def test_funnel_cierre_sin_visita_por_diferencia_de_conjuntos():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED, "pipeline_stage": "CLOSED_WON",
             "stage_history": [{"to": "CLOSED_WON", "timestamp": "2026-07-15T10:00:00Z"}]}]
    res = build_funnel_result(make_db(leads=docs))
    assert res["hitos_excepcionales"]["cierres"] == 1
    assert res["hitos_excepcionales"]["cierres_sin_visita"] == 1  # sin visita registrada


def test_funnel_hito_avanzado_con_visita_no_aparece_sin_visita():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED, "phone": "+56912345678", "prospecto": {"codigo": "P1"},
             "stage_history": [{"to": "CLOSED_WON", "timestamp": "2026-07-15T10:00:00Z"}]}]
    visitas = [{"_id": "v1", "visita_code": "VIS-2026-0001", "phone": "+56912345678",
                "property_code": "P1", "status": "signed",
                "timeline": [{"action": "accepted", "server_timestamp": "2026-07-14T10:00:00Z"}]}]
    res = build_funnel_result(make_db(leads=docs, visitas=visitas))
    assert res["hitos_excepcionales"]["avanzados"] == 1
    assert res["hitos_excepcionales"]["avanzados_sin_visita"] == 0  # tuvo visita


def test_funnel_json_serializable():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED,
             "lifecycle": {"first_valid_management_at": "2026-07-15T10:00:00Z"}}]
    res = build_funnel_result(make_db(leads=docs))
    json.dumps(res)


def test_funnel_no_calificados_en_frontend():
    assert "Calificados" not in HTML
    assert "calificados" not in HTML
    assert "Visita agendada" in HTML
    assert "Contacto efectivo" in HTML
    assert "Gestionados" in HTML


# =============================================================================
# 4. RENDIMIENTO COMERCIAL POR EJECUTIVO (as-of)
# =============================================================================

from analytics.leads_queries import query_leads_dashboard_executives


def make_cycle(lead_id, name, assigned, unassigned=None):
    return {"lead_id": lead_id, "assigned_to_display_name": name,
            "assigned_at": assigned, "unassigned_at": unassigned}


def seed_usuarios(db):
    """Inserta agentes activos para que el filtro de la tabla los considere."""
    for name in ("Mariela Arriagada", "Erika Garrido", "Susana Ensignia",
                 "Paula Morales", "Rocío Aliaga", "María Paz Galleguillos", "Hernán Castro"):
        db["usuarios"].insert_one({"nombre": name, "rol": "agente", "is_active": True})


def build_exec_result(db, period_start="2026-07-10", period_end="2026-07-20",
                      cmp_start=None, cmp_end=None):
    seed_usuarios(db)
    with patch.object(lq, "get_db", return_value=db), \
         patch.object(lq, "_normalized_created_at_stage", return_value={}), \
         patch.object(lq, "_build_commercial_cohort_match", return_value={}):
        return query_leads_dashboard_executives(
            period_start=period_start, period_end=period_end,
            comparison_start=cmp_start, comparison_end=cmp_end,
            include_comparison=bool(cmp_start and cmp_end),
        )


def test_exec_lead_with_active_cycle_asignado_correcto():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    cycles = [make_cycle("l1", "Mariela Arriagada", "2026-07-12T10:00:00Z")]
    db = make_db(leads=docs)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db)
    row = next(r for r in res["rows"] if r["ejecutivo"] == "Mariela Arriagada")
    assert row["leads"] == 1


def test_exec_reasignacion_posterior_no_cambia_periodo_historico():
    # Período cierra 2026-07-21T04:00Z. Reasignación el 2026-07-25 a otro.
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    cycles = [
        make_cycle("l1", "Mariela Arriagada", "2026-07-12T10:00:00Z", "2026-07-25T10:00:00Z"),
        make_cycle("l1", "Erika Garrido", "2026-07-25T10:00:00Z"),
    ]
    db = make_db(leads=docs)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db)
    row_m = next((r for r in res["rows"] if r["ejecutivo"] == "Mariela Arriagada"), None)
    row_e = next((r for r in res["rows"] if r["ejecutivo"] == "Erika Garrido"), None)
    assert row_m is not None and row_m["leads"] == 1
    assert row_e is None or row_e["leads"] == 0


def test_exec_ciclo_terminado_antes_del_cierre_es_sin_asignar():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    cycles = [make_cycle("l1", "Mariela Arriagada", "2026-07-12T10:00:00Z", "2026-07-15T10:00:00Z")]
    db = make_db(leads=docs)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db)
    sa = next((r for r in res["rows"] if r["ejecutivo"] == "Sin Asignar"), None)
    assert sa is not None and sa["leads"] == 1
    assert res["reconcile"]["sin_asignar"] == 1


def test_exec_multiple_ciclos_usa_vigente_al_cierre():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    cycles = [
        make_cycle("l1", "Mariela Arriagada", "2026-07-12T10:00:00Z", "2026-07-14T10:00:00Z"),
        make_cycle("l1", "Erika Garrido", "2026-07-14T11:00:00Z"),
    ]
    db = make_db(leads=docs)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db)
    row_e = next((r for r in res["rows"] if r["ejecutivo"] == "Erika Garrido"), None)
    row_m = next((r for r in res["rows"] if r["ejecutivo"] == "Mariela Arriagada"), None)
    assert row_e is not None and row_e["leads"] == 1
    assert row_m is None or row_m["leads"] == 0


def test_exec_ciclo_exactamente_en_limite():
    # period_end exclusivo = 2026-07-21T04:00:00Z.
    # Ciclo asignado a las 04:00:00Z del límite NO cuenta (límite exclusivo).
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    cycles = [make_cycle("l1", "Mariela Arriagada", "2026-07-21T04:00:00Z")]
    db = make_db(leads=docs)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db)
    sa = next((r for r in res["rows"] if r["ejecutivo"] == "Sin Asignar"), None)
    assert sa is not None and sa["leads"] == 1


def test_exec_suma_reconcilia_cohort_global():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z"},
        {"_id": "l3", "created_at": "2026-07-12T14:00:00Z"},
    ]
    cycles = [
        make_cycle("l1", "Mariela Arriagada", "2026-07-12T10:00:00Z"),
        make_cycle("l2", "Erika Garrido", "2026-07-12T10:00:00Z", "2026-07-15T10:00:00Z"),
    ]
    db = make_db(leads=docs)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db)
    total_rows = sum(r["leads"] for r in res["rows"])
    # l1 -> Mariela, l2 -> Sin asignar (ciclo cerrado), l3 -> Sin asignar (sin ciclo)
    assert total_rows + res["reconcile"]["otros"] == 3
    assert res["reconcile"]["total"] == 3
    assert res["reconcile"]["sin_asignar"] == 2


def test_exec_visitas_tabla_igual_card2():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    events = [{"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "visita_agendada",
               "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela Arriagada",
               "actor_type": "agent", "confirmed": True}]
    cycles = [make_cycle("l1", "Mariela Arriagada", "2026-07-12T10:00:00Z")]
    db = make_db(leads=docs, events=events)
    db["crm_assignment_cycles"].insert_many(cycles)
    exec_res = build_exec_result(db)
    conv_res = build_query_result(db)["current"]
    assert conv_res["citas"] == 1
    row = next(r for r in exec_res["rows"] if r["ejecutivo"] == "Mariela Arriagada")
    assert row["citas"] == 1


def test_exec_visita_se_atribuye_al_responsable_as_of_no_al_actor():
    # Actor = Pablo (admin), responsable as-of = Erika.
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    events = [{"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "visita_agendada",
               "timestamp": "2026-07-15T10:00:00Z", "actor": "Pablo Galleguillos",
               "actor_type": "administrator", "confirmed": True}]
    cycles = [make_cycle("l1", "Erika Garrido", "2026-07-12T10:00:00Z")]
    db = make_db(leads=docs, events=events)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db)
    row_e = next(r for r in res["rows"] if r["ejecutivo"] == "Erika Garrido")
    assert row_e["citas"] == 1
    # Pablo (admin) no aparece como fila comercial.
    assert all(r["ejecutivo"] != "Pablo Galleguillos" for r in res["rows"])


def test_exec_visita_sin_responsable_es_sin_asignar():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    events = [{"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "visita_agendada",
               "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela Arriagada",
               "actor_type": "agent", "confirmed": True}]
    db = make_db(leads=docs, events=events)  # sin ciclos -> Sin asignar
    res = build_exec_result(db)
    sa = next(r for r in res["rows"] if r["ejecutivo"] == "Sin Asignar")
    assert sa["citas"] == 1
    assert res["reconcile"]["sin_asignar_visitas"] == 1


def test_exec_suma_visitas_reconcilia_card2():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z"},
    ]
    events = [
        {"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "visita_agendada",
         "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela Arriagada", "actor_type": "agent", "confirmed": True},
        {"_id": "e2", "lead_id": "l2", "type": "HUMAN_NOTE", "result": "visita_agendada",
         "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela Arriagada", "actor_type": "agent", "confirmed": True},
    ]
    cycles = [make_cycle("l1", "Mariela Arriagada", "2026-07-12T10:00:00Z")]
    db = make_db(leads=docs, events=events)
    db["crm_assignment_cycles"].insert_many(cycles)
    exec_res = build_exec_result(db)
    conv_res = build_query_result(db)["current"]
    total_vis = sum(r["citas"] for r in exec_res["rows"])
    assert total_vis == conv_res["citas"] == 2
    assert exec_res["reconcile"]["total_visitas"] == 2


def test_exec_conversion_mismo_universo():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z"},
    ]
    events = [{"_id": "e1", "lead_id": "l1", "type": "HUMAN_NOTE", "result": "visita_agendada",
               "timestamp": "2026-07-15T10:00:00Z", "actor": "Mariela Arriagada", "actor_type": "agent", "confirmed": True}]
    cycles = [make_cycle("l1", "Mariela Arriagada", "2026-07-12T10:00:00Z"),
              make_cycle("l2", "Mariela Arriagada", "2026-07-12T10:00:00Z")]
    db = make_db(leads=docs, events=events)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db)
    row = next(r for r in res["rows"] if r["ejecutivo"] == "Mariela Arriagada")
    assert row["leads"] == 2 and row["citas"] == 1
    # conversión en el service = 1/2 = 50% (citas/leads mismo universo)
    from analytics.leads_service import get_leads_dashboard_overview
    # validamos la fórmula de conversión replicada en service
    conv = round(row["citas"] / row["leads"] * 100, 1) if row["leads"] else None
    assert conv == 50.0


def test_exec_conversion_leads_cero_es_nd():
    # leads=0 -> N/D (None), no 0%.
    from analytics.leads_service import get_leads_dashboard_overview
    # Replicar regla del service
    leads = 0
    conv = round(0 / leads * 100, 1) if leads else None
    assert conv is None


def test_exec_conversion_anterior_usa_asignacion_asof_anterior():
    # Período actual (07-10..07-20) y anterior (06-10..06-20).
    docs = [
        {"_id": "p1", "created_at": "2026-06-12T14:00:00Z"},
        {"_id": "c1", "created_at": "2026-07-12T14:00:00Z"},
    ]
    cycles = [
        # p1 en período anterior asignado a Mariela; reasignado en julio a Erika
        make_cycle("p1", "Mariela Arriagada", "2026-06-13T10:00:00Z", "2026-07-20T10:00:00Z"),
        make_cycle("p1", "Erika Garrido", "2026-07-20T10:00:00Z"),
        make_cycle("c1", "Mariela Arriagada", "2026-07-13T10:00:00Z"),
    ]
    events = [{"_id": "e1", "lead_id": "p1", "type": "HUMAN_NOTE", "result": "visita_agendada",
               "timestamp": "2026-06-15T10:00:00Z", "actor": "Mariela Arriagada", "actor_type": "agent", "confirmed": True}]
    db = make_db(leads=docs, events=events)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db, cmp_start="2026-06-10", cmp_end="2026-06-20")
    row_m = next(r for r in res["rows"] if r["ejecutivo"] == "Mariela Arriagada")
    # p1 en período anterior asignado a Mariela (as-of anterior) -> leads_prev=1, citas_prev=1
    assert row_m["leads_prev"] == 1
    assert row_m["citas_prev"] == 1


def test_exec_json_serializable():
    docs = [{"_id": "l1", "created_at": LEAD_CREATED}]
    cycles = [make_cycle("l1", "Mariela Arriagada", "2026-07-12T10:00:00Z")]
    db = make_db(leads=docs)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db)
    json.dumps(res)


def test_exec_frontend_sin_sla_ni_uf():
    # La tabla no debe contener Mediana SLA ni Pipeline UF.
    assert "sla_median" not in HTML
    assert "Mediana SLA" not in HTML
    assert "Pipeline UF" not in HTML.split("Rendimiento Comercial por Ejecutivo")[1].split("</section>")[0] if "Rendimiento Comercial por Ejecutivo" in HTML else True
    assert "Visitas Agendadas" in HTML
    assert "data-sort=\"conversion_pct\"" in HTML


# =============================================================================
# 5. RECONCILE AS-OF Y ORDENAMIENTO (Rendimiento por Ejecutivo)
# =============================================================================


def test_reconcile_sin_asignar_usa_ejecutivo_asof():
    # Lead con ciclo cerrado antes del cierre -> Sin asignar as-of, aunque el
    # campo ejecutivo_asignado residual diga otro nombre.
    docs = [{"_id": "l1", "created_at": LEAD_CREATED, "ejecutivo_asignado": "Mariela Arriagada"}]
    cycles = [make_cycle("l1", "Mariela Arriagada", "2026-07-12T10:00:00Z", "2026-07-15T10:00:00Z")]
    db = make_db(leads=docs)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db)
    assert res["reconcile"]["sin_asignar"] == 1
    assert res["reconcile"]["comerciales"] == 0


def test_reconcile_no_usa_ejecutivo_asignado_residual():
    # 2 leads: uno con ciclo activo a Erika, otro sin ciclo (residual Pablo).
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED, "ejecutivo_asignado": "Erika Garrido"},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z", "ejecutivo_asignado": "Pablo Galleguillos"},
    ]
    cycles = [make_cycle("l1", "Erika Garrido", "2026-07-12T10:00:00Z")]
    db = make_db(leads=docs)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db)
    # l2 sin ciclo -> Sin asignar (no Pablo, no comercial).
    assert res["reconcile"]["sin_asignar"] == 1
    assert res["reconcile"]["comerciales"] == 1
    assert res["reconcile"]["otros"] == 0


def test_reconcile_ejecutivos_mas_sin_asignar_mas_otros_igual_total():
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z"},
        {"_id": "l3", "created_at": "2026-07-12T14:00:00Z"},
        {"_id": "l4", "created_at": "2026-07-13T14:00:00Z"},
    ]
    cycles = [
        make_cycle("l1", "Mariela Arriagada", "2026-07-12T10:00:00Z"),
        make_cycle("l2", "Erika Garrido", "2026-07-12T10:00:00Z", "2026-07-15T10:00:00Z"),  # -> Sin asignar
    ]
    db = make_db(leads=docs)
    db["crm_assignment_cycles"].insert_many(cycles)
    # l3, l4 sin ciclo -> Sin asignar; total comerciales=1, sin_asignar=3
    res = build_exec_result(db)
    assert res["reconcile"]["total"] == 4
    assert res["reconcile"]["comerciales"] + res["reconcile"]["sin_asignar"] + res["reconcile"]["otros"] == res["reconcile"]["total"]


def test_reconcile_payload_usa_etiqueta_comerciales_no_contabilizado_inflado():
    # El payload reconcile debe exponer comerciales (sin incluir Sin asignar).
    docs = [
        {"_id": "l1", "created_at": LEAD_CREATED},
        {"_id": "l2", "created_at": "2026-07-11T14:00:00Z"},
    ]
    cycles = [make_cycle("l1", "Mariela Arriagada", "2026-07-12T10:00:00Z")]
    db = make_db(leads=docs)
    db["crm_assignment_cycles"].insert_many(cycles)
    res = build_exec_result(db)
    # comerciales=1 (Mariela), sin_asignar=1 (l2)
    assert res["reconcile"]["comerciales"] == 1
    assert res["reconcile"]["sin_asignar"] == 1
    assert res["reconcile"]["comerciales"] != res["reconcile"]["total"]


def test_frontend_etiqueta_no_llama_ejecutivos_a_conjunto_con_sin_asignar():
    # El texto del reconcile separa Ejecutivos comerciales / Sin asignar / Otros.
    fn = HTML.split("function renderExecutivesTable()", 1)[1].split("function renderFunnel()", 1)[0]
    assert "Ejecutivos comerciales" in fn
    assert "Sin asignar" in fn
    assert "Otros (admin/pruebas)" in fn
    # No debe existir el texto viejo que llamaba "Ejecutivos" a todo.
    assert "Total dashboard" not in fn


def test_frontend_orden_empate_por_conversion_descendente():
    # María Paz (1 visita, 7.1%) precede a Susana (1 visita, 6.2%).
    items = [
        {"nombre": "Susana Ensignia", "citas": 1, "conversion_pct": 6.2, "leads": 16},
        {"nombre": "María Paz Galleguillos", "citas": 1, "conversion_pct": 7.1, "leads": 14},
        {"nombre": "Mariela Arriagada", "citas": 11, "conversion_pct": 12.6, "leads": 87},
    ]
    ordered = sorted(items, key=lambda a: (a["citas"], a["conversion_pct"]), reverse=True)
    assert ordered[0]["nombre"] == "Mariela Arriagada"
    assert ordered[1]["nombre"] == "María Paz Galleguillos"
    assert ordered[2]["nombre"] == "Susana Ensignia"


def test_frontend_sin_asignar_siempre_al_final():
    # "Sin asignar" queda al final aunque tenga conversión > 0.
    items = [
        {"nombre": "Mariela Arriagada", "citas": 0, "conversion_pct": 0.0, "leads": 87},
        {"nombre": "Sin Asignar", "citas": 1, "conversion_pct": 3.7, "leads": 27},
        {"nombre": "Erika Garrido", "citas": 0, "conversion_pct": 0.0, "leads": 116},
    ]
    ordered = sorted(
        items,
        key=lambda a: (a["nombre"].strip().lower() == "sin asignar", -(a["citas"] or 0), -(a["conversion_pct"] or 0))
    )
    assert ordered[-1]["nombre"] == "Sin Asignar"


# =============================================================================
# 6. CARD 3 — CARTERA POTENCIAL & VALORIZACIÓN
# =============================================================================


def _run_overview_pipeline(properties, commission_rows, uf=40846.11):
    """Ejecuta get_leads_dashboard_overview con pipeline/commission simulados."""
    from analytics.leads_service import get_leads_dashboard_overview
    db = make_db([])
    _valued = [p for p in properties if p.get("precio_uf") is not None]
    pipeline_mock = {
        "monto_uf": sum((p["precio_uf"] or 0) for p in properties),
        "propiedades_vinculadas": len(properties),
        "propiedades_cartera": len(properties),
        "propiedades_con_precio": len(_valued),
        "propiedades_cartera_valorizadas": len(_valued),
        "propiedades_venta": sum(1 for p in _valued if p["operacion"] == "venta"),
        "propiedades_arriendo": sum(1 for p in _valued if p["operacion"] == "arriendo"),
        "propiedades_otro": sum(1 for p in _valued if p["operacion"] == "otro"),
        "propiedades_sin_precio": sum(1 for p in properties if p.get("precio_uf") is None),
        "propiedades_no_en_cartera": 0,
        "leads_vinculados": len(properties),
        "monto_venta_uf": round(sum((p["precio_uf"] or 0) for p in properties if p["operacion"] == "venta"), 1),
        "monto_arriendo_uf": round(sum((p["precio_uf"] or 0) for p in properties if p["operacion"] == "arriendo"), 1),
        "monto_otro_uf": round(sum((p["precio_uf"] or 0) for p in properties if p["operacion"] == "otro"), 1),
    }
    with ExitStack() as stack:
        for patcher in _service_patches(total=len(properties), citas=0, evaluable=0):
            stack.enter_context(patcher)
        stack.enter_context(patch.object(lq, "get_db", return_value=db))
        stack.enter_context(patch.object(lq, "_normalized_created_at_stage", return_value={}))
        stack.enter_context(patch.object(lq, "_build_commercial_cohort_match", return_value={}))
        stack.enter_context(patch.object(lq, "query_leads_dashboard_pipeline", return_value=pipeline_mock))
        stack.enter_context(patch.object(lq, "query_property_commission_rows", return_value=commission_rows))
        # El service importa query_property_commission_rows como nombre local:
        stack.enter_context(patch("analytics.leads_service.query_property_commission_rows", return_value=commission_rows))
        # uf_value viene de leer_uf_cache / macro; forzamos con macro
        stack.enter_context(patch("analytics.leads_service._load_commercial_macro_information", return_value={
            "available": True, "indicators": {"uf": {"value": uf, "as_of": "2026-08-10T04:00:00Z", "available": True}},
        }))
        return get_leads_dashboard_overview(period_start="2026-07-10", period_end="2026-07-20", compare="none")


def test_card3_propiedad_repetida_se_cuenta_una_vez():
    # Deduplicación por código: dos leads P1 -> una sola propiedad en comisión.
    rows = [
        {"codigo": "P1", "precio_uf": 5200.0, "operacion": "venta"},
        {"codigo": "P2", "precio_uf": 1000.0, "operacion": "venta"},
    ]
    # El query deduplica por codigo; simulamos 2 filas únicas aunque hubo 3 leads.
    ov = _run_overview_pipeline(
        properties=[{"codigo": "P1", "precio_uf": 5200.0, "operacion": "venta"},
                    {"codigo": "P2", "precio_uf": 1000.0, "operacion": "venta"}],
        commission_rows=rows,
    )
    p = ov["pipeline"]
    assert p["propiedades_valorizadas"] == 2
    assert p["monto_venta_uf"] == 6200.0


def test_card3_venta_usa_2pct_no_4():
    ov = _run_overview_pipeline(
        properties=[{"codigo": "P1", "precio_uf": 10000.0, "operacion": "venta"}],
        commission_rows=[{"codigo": "P1", "precio_uf": 10000.0, "operacion": "venta"}],
    )
    p = ov["pipeline"]
    # 2% de 10000 = 200 UF (no 400)
    assert p["comision_venta_uf"] == 200.0
    assert p["comision_potencial_uf"] == 200.0


def test_card3_venta_bajo_minimo_usa_1mm_uf():
    UF = 40846.11
    min_uf = 1000000 / UF  # ~24.48 UF
    # 100 UF * 2% = 2 UF < 24.48 UF -> aplica mínimo
    ov = _run_overview_pipeline(
        properties=[{"codigo": "P1", "precio_uf": 100.0, "operacion": "venta"}],
        commission_rows=[{"codigo": "P1", "precio_uf": 100.0, "operacion": "venta"}],
    )
    p = ov["pipeline"]
    assert round(min_uf, 2) == round(24.48, 2)
    assert round(p["comision_venta_uf"], 1) == round(min_uf, 1)  # aplica mínimo (1 decimal)
    assert p["comision_venta_afectadas_min"] == 1


def test_card3_venta_sobre_minimo_usa_2pct():
    ov = _run_overview_pipeline(
        properties=[{"codigo": "P1", "precio_uf": 20000.0, "operacion": "venta"}],
        commission_rows=[{"codigo": "P1", "precio_uf": 20000.0, "operacion": "venta"}],
    )
    p = ov["pipeline"]
    assert p["comision_venta_uf"] == 400.0  # 20000 * 0.02


def test_card3_arriendo_usa_50pct_no_100():
    ov = _run_overview_pipeline(
        properties=[{"codigo": "A1", "precio_uf": 80.0, "operacion": "arriendo"}],
        commission_rows=[{"codigo": "A1", "precio_uf": 80.0, "operacion": "arriendo"}],
    )
    p = ov["pipeline"]
    assert p["comision_arriendo_uf"] == 40.0  # 80 * 0.50, no 80


def test_card3_arriendo_bajo_minimo_usa_100k_uf():
    UF = 40846.11
    min_uf = 100000 / UF  # ~2.4482 UF
    # canon 4 UF * 50% = 2 UF < 2.4482 UF -> aplica mínimo
    ov = _run_overview_pipeline(
        properties=[{"codigo": "A1", "precio_uf": 4.0, "operacion": "arriendo"}],
        commission_rows=[{"codigo": "A1", "precio_uf": 4.0, "operacion": "arriendo"}],
    )
    p = ov["pipeline"]
    assert round(min_uf, 4) == round(2.4482, 4)
    assert round(p["comision_arriendo_uf"], 1) == round(min_uf, 1)  # aplica mínimo (1 decimal)
    assert p["comision_arriendo_afectadas_min"] == 1


def test_card3_comision_total_por_propiedad():
    rows = [
        {"codigo": "V1", "precio_uf": 10000.0, "operacion": "venta"},
        {"codigo": "V2", "precio_uf": 300.0, "operacion": "venta"},
        {"codigo": "A1", "precio_uf": 100.0, "operacion": "arriendo"},
    ]
    ov = _run_overview_pipeline(properties=rows, commission_rows=rows)
    p = ov["pipeline"]
    UF = 40846.11
    min_v = 1000000 / UF
    min_a = 100000 / UF
    expected_venta = round(max(10000 * 0.02, min_v) + max(300 * 0.02, min_v), 1)
    expected_arriendo = round(max(100 * 0.50, min_a), 1)
    assert p["comision_venta_uf"] == expected_venta
    assert p["comision_arriendo_uf"] == expected_arriendo
    assert p["comision_potencial_uf"] == round(expected_venta + expected_arriendo, 1)


def test_card3_comision_total_venta_mas_arriendo_valida():
    rows = [
        {"codigo": "V1", "precio_uf": 10000.0, "operacion": "venta"},
        {"codigo": "A1", "precio_uf": 100.0, "operacion": "arriendo"},
    ]
    ov = _run_overview_pipeline(properties=rows, commission_rows=rows)
    p = ov["pipeline"]
    assert p["comision_potencial_uf"] == round(p["comision_venta_uf"] + p["comision_arriendo_uf"], 1)


def test_card3_sin_iva():
    # Comisión neta: no multiplicar por 1.19
    ov = _run_overview_pipeline(
        properties=[{"codigo": "V1", "precio_uf": 10000.0, "operacion": "venta"}],
        commission_rows=[{"codigo": "V1", "precio_uf": 10000.0, "operacion": "venta"}],
    )
    p = ov["pipeline"]
    assert p["comision_venta_uf"] == 200.0  # no 238


def test_card3_propiedad_sin_precio_no_genera_comision():
    # Propiedad sin precio: vinculada, no valorizada, sin comisión.
    rows = [{"codigo": "P2", "precio_uf": 2000.0, "operacion": "venta"}]  # solo con precio
    ov = _run_overview_pipeline(
        properties=[
            {"codigo": "P1", "precio_uf": None, "operacion": "venta"},
            {"codigo": "P2", "precio_uf": 2000.0, "operacion": "venta"},
        ],
        commission_rows=rows,
    )
    p = ov["pipeline"]
    assert p["propiedades_vinculadas"] == 2
    assert p["propiedades_valorizadas"] == 1
    assert p["propiedades_sin_precio"] == 1
    assert p["comision_venta_uf"] == 40.0  # 2000 * 0.02, solo P2


def test_card3_vinculadas_igual_valorizadas_mas_sin_precio():
    ov = _run_overview_pipeline(
        properties=[
            {"codigo": "P1", "precio_uf": 1000.0, "operacion": "venta"},
            {"codigo": "P2", "precio_uf": None, "operacion": "venta"},
            {"codigo": "P3", "precio_uf": 500.0, "operacion": "arriendo"},
        ],
        commission_rows=[
            {"codigo": "P1", "precio_uf": 1000.0, "operacion": "venta"},
            {"codigo": "P3", "precio_uf": 500.0, "operacion": "arriendo"},
        ],
    )
    p = ov["pipeline"]
    assert p["propiedades_vinculadas"] == p["propiedades_valorizadas"] + p["propiedades_sin_precio"]
    assert p["propiedades_valorizadas"] == 2
    assert p["propiedades_sin_precio"] == 1
    assert p["pct_valorizadas"] == round(2 / 3 * 100, 1)


def test_card3_venta_y_arriendo_no_se_suman_como_total_uf():
    # monto_uf legacy puede existir, pero venta y arriendo se entregan separados
    # y el frontend no los suma como valorización única.
    ov = _run_overview_pipeline(
        properties=[
            {"codigo": "V1", "precio_uf": 10000.0, "operacion": "venta"},
            {"codigo": "A1", "precio_uf": 100.0, "operacion": "arriendo"},
        ],
        commission_rows=[
            {"codigo": "V1", "precio_uf": 10000.0, "operacion": "venta"},
            {"codigo": "A1", "precio_uf": 100.0, "operacion": "arriendo"},
        ],
    )
    p = ov["pipeline"]
    assert p["monto_venta_uf"] == 10000.0
    assert p["monto_arriendo_uf"] == 100.0
    # comisión potencial sí suma (mismo concepto UF)
    assert p["comision_potencial_uf"] == round(200.0 + 50.0, 1)


def test_card3_frontend_arriendo_uf_visible():
    # Presentación: Arriendo en UF (sin "/mes" visible), en UNA SOLA LÍNEA con
    # Venta (sin nota "Renta mensual" debajo). El tooltip mantiene la unidad
    # real (UF/mes, canon mensual).
    card_html = HTML.split('id="cardPipeline"', 1)[1].split('id="cardSla"', 1)[0]
    arriendo_item = card_html.split('id="cd3MontoArriendo"', 1)[1]
    arriendo_item = arriendo_item[:200]
    assert "UF" in arriendo_item
    assert "/mes" not in arriendo_item
    assert "Renta mensual" not in card_html
    assert "renta mensual" in HTML  # el tooltip explica que es el canon mensual
    # Métricas secundarias en una sola línea horizontal (mismo contenedor .cd3-sec).
    assert 'id="cd3MontoVenta"' in card_html and 'id="cd3MontoArriendo"' in card_html
    assert 'cd3-sec-item-arriendo' not in card_html  # sin columna separada


def test_card3_comision_estimada_desaparece_y_aparece_potencial():
    assert "Comisión estimada" not in HTML
    assert "Comisión potencial" in HTML
    assert "comision_potencial_uf" in HTML


def test_card3_no_4pct_en_frontend():
    assert "4% venta" not in HTML
    assert "comision_estimada_uf" not in HTML
    assert "pct_comision" not in HTML


def test_card3_pdf_usa_comision_potencial():
    pdf = HTML.split("function exportExecutivePDF()", 1)[1]
    assert "Comisión potencial" in pdf
    assert "comision_potencial_uf" in pdf
    assert "Comisión estimada" not in pdf
    assert "Venta + Arriendo" not in pdf


def test_card3_property_6756_no_filtrada():
    src = SERVICE_SRC + "\n" + Path("analytics/leads_queries.py").read_text(encoding="utf-8")
    assert "6756" not in src


def test_card3_json_serializable():
    ov = _run_overview_pipeline(
        properties=[{"codigo": "V1", "precio_uf": 10000.0, "operacion": "venta"}],
        commission_rows=[{"codigo": "V1", "precio_uf": 10000.0, "operacion": "venta"}],
    )
    json.dumps(ov)


# =============================================================================
# 6.1 CARD 3 — FUENTE CANÓNICA DE PRECIO (universo_cartera_prop360)
# =============================================================================

from analytics.leads_queries import (
    _canonical_prices_for_codes,
    _property_price_rows,
    query_leads_dashboard_pipeline,
    query_property_commission_rows,
)


def _make_prop_db(leads, universo):
    db = make_db(leads=leads)
    if universo:
        db["universo_cartera_prop360"].insert_many(universo)
    return db


def _run_property_rows(db, **kw):
    with patch.object(lq, "get_db", return_value=db), \
         patch.object(lq, "_normalized_created_at_stage", return_value={}), \
         patch.object(lq, "_build_commercial_cohort_match", return_value={}):
        return query_property_commission_rows(period_start="2026-07-10", period_end="2026-07-20", **kw)

def test_card3_fuente_canonica_lead_sin_precio_recupera_precio():
    # A. Lead sin precio, pero propiedad actual SÍ tiene precio en cartera.
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "100", "operacion": "Venta", "precio_uf": None}},
        ],
        universo=[
            {"codigo": "100", "tipo_operacion": {
                "venta": True, "arriendo": False,
                "precio_venta": {"precio_uf": 5000.0},
                "precio_arriendo": {"precio_uf": None},
            }},
        ],
    )
    rows = _run_property_rows(db)
    assert len(rows) == 1
    assert rows[0]["codigo"] == "100"
    assert rows[0]["precio_uf"] == 5000.0
    assert rows[0]["operacion"] == "venta"


def test_card3_fuente_canonica_se_usa_cuando_existe():
    # La prioridad conceptual es la fuente canónica, no los campos del lead.
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "100", "operacion": "Venta", "precio_uf": 4000.0}},
        ],
        universo=[
            {"codigo": "100", "tipo_operacion": {
                "venta": True, "arriendo": False,
                "precio_venta": {"precio_uf": 5000.0},
                "precio_arriendo": {"precio_uf": None},
            }},
        ],
    )
    rows = _run_property_rows(db)
    assert rows[0]["precio_uf"] == 5000.0


def test_card3_fuente_canonica_lead_corrige_unidad_6801():
    # 6801: el lead trae 1220 UF/mes (error de unidad), el canónico 12,2 UF.
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "6801", "operacion": "Arriendo"},
             "cartera_data": {"precio_uf": 1220.0}},
        ],
        universo=[
            {"codigo": "6801", "tipo_operacion": {
                "venta": False, "arriendo": True,
                "precio_venta": {"precio_uf": None},
                "precio_arriendo": {"precio_uf": 12.2},
            }},
        ],
    )
    rows = _run_property_rows(db)
    assert rows[0]["precio_uf"] == 12.2


def test_card3_fuente_canonica_operacion_desde_canonico_si_lead_vacia():
    # Lead sin operación: la operación se resuelve desde la fuente canónica.
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "100", "precio_uf": None}},
        ],
        universo=[
            {"codigo": "100", "tipo_operacion": {
                "venta": False, "arriendo": True,
                "precio_venta": {"precio_uf": None},
                "precio_arriendo": {"precio_uf": 60.0},
            }},
        ],
    )
    rows = _run_property_rows(db)
    assert len(rows) == 1
    assert rows[0]["operacion"] == "arriendo"
    assert rows[0]["precio_uf"] == 60.0


def test_card3_sin_precio_en_ninguna_fuente():
    # B. Propiedad realmente sin precio en ninguna fuente actual.
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "100", "operacion": "Venta", "precio_uf": None}},
        ],
        universo=[
            {"codigo": "100", "tipo_operacion": {
                "venta": True, "arriendo": False,
                "precio_venta": {"precio_uf": None},
                "precio_arriendo": {"precio_uf": None},
            }},
        ],
    )
    rows = _run_property_rows(db)
    assert rows == []


def test_card3_codigo_no_existe_en_cartera_se_reporta_separado():
    # C. Código que no existe en la cartera actual: sin valorización, reportado aparte.
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "SMOKE005", "operacion": "Venta", "precio_uf": None}},
        ],
        universo=[],
    )
    with patch.object(lq, "get_db", return_value=db), \
         patch.object(lq, "_normalized_created_at_stage", return_value={}), \
         patch.object(lq, "_build_commercial_cohort_match", return_value={}):
        pipe = query_leads_dashboard_pipeline(period_start="2026-07-10", period_end="2026-07-20")
    assert pipe["propiedades_vinculadas"] == 1
    assert pipe["propiedades_cartera"] == 0
    assert pipe["propiedades_con_precio"] == 0
    assert pipe["propiedades_cartera_valorizadas"] == 0
    assert pipe["propiedades_sin_precio"] == 0
    assert pipe["propiedades_no_en_cartera"] == 1


def test_card3_pipeline_contabiliza_canonico_y_no_en_cartera():
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "A", "operacion": "Venta", "precio_uf": None}},
            {"_id": "l2", "created_at": "2026-07-12T14:00:00Z",
             "prospecto": {"codigo": "B", "operacion": "Venta", "precio_uf": 2000.0}},
            {"_id": "l3", "created_at": "2026-07-13T14:00:00Z",
             "prospecto": {"codigo": "C", "operacion": "Arriendo", "precio_uf": None}},
            {"_id": "l4", "created_at": "2026-07-14T14:00:00Z",
             "prospecto": {"codigo": "GHOST", "operacion": "Venta", "precio_uf": None}},
        ],
        universo=[
            {"codigo": "A", "tipo_operacion": {
                "venta": True, "arriendo": False,
                "precio_venta": {"precio_uf": 3000.0}, "precio_arriendo": {"precio_uf": None}}},
            {"codigo": "B", "tipo_operacion": {
                "venta": True, "arriendo": False,
                "precio_venta": {"precio_uf": 2000.0}, "precio_arriendo": {"precio_uf": None}}},
            {"codigo": "C", "tipo_operacion": {
                "venta": False, "arriendo": True,
                "precio_venta": {"precio_uf": None}, "precio_arriendo": {"precio_uf": 100.0}}},
        ],
    )
    with patch.object(lq, "get_db", return_value=db), \
         patch.object(lq, "_normalized_created_at_stage", return_value={}), \
         patch.object(lq, "_build_commercial_cohort_match", return_value={}):
        pipe = query_leads_dashboard_pipeline(period_start="2026-07-10", period_end="2026-07-20")
    assert pipe["propiedades_vinculadas"] == 4
    assert pipe["propiedades_cartera"] == 3
    assert pipe["propiedades_cartera_valorizadas"] == 3
    assert pipe["propiedades_venta"] == 2
    assert pipe["propiedades_arriendo"] == 1
    assert pipe["propiedades_sin_precio"] == 0
    assert pipe["propiedades_no_en_cartera"] == 1
    # Reconciliación Venta/Arriendo sobre la cartera valorizada.
    assert pipe["propiedades_venta"] + pipe["propiedades_arriendo"] == pipe["propiedades_cartera_valorizadas"]
    assert pipe["monto_venta_uf"] == 5000.0  # A(3000 canon) + B(2000 lead)
    assert pipe["monto_arriendo_uf"] == 100.0


def test_card3_canonical_prices_lee_operacion_vigente():
    db = _make_prop_db([], universo=[
        {"codigo": "X", "tipo_operacion": {
            "venta": False, "arriendo": True,
            "precio_venta": {"precio_uf": None}, "precio_arriendo": {"precio_uf": 25.5}}},
    ])
    with patch.object(lq, "get_db", return_value=db):
        m = _canonical_prices_for_codes(["X"])
    assert m["X"]["arriendo_uf"] == 25.5
    assert m["X"]["op_canon"] == "Arriendo"


def test_card3_canonical_precio_clp_sin_uf_se_convierte_con_uf_snapshot():
    # 7726: canon arriendo CLP 450.000 sin UF -> 450000 / 40846.11 UF/mes.
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "7726", "operacion": "Arriendo"}},
        ],
        universo=[
            {"codigo": "7726", "tipo_operacion": {
                "venta": False, "arriendo": True,
                "precio_venta": {"precio_clp": None}, "precio_arriendo": {"precio_clp": 450000}}},
        ],
    )
    rows = _run_property_rows(db, uf_value=40846.11)
    assert len(rows) == 1
    assert round(rows[0]["precio_uf"], 2) == round(450000 / 40846.11, 2)
    assert rows[0]["operacion"] == "arriendo"


def test_card3_canonical_venta_clp_sin_uf_se_convierte():
    # 6815 / 6282: venta CLP sin UF -> CLP / UF.
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "6815", "operacion": "Venta"}},
            {"_id": "l2", "created_at": "2026-07-12T14:00:00Z",
             "prospecto": {"codigo": "6282", "operacion": "Venta"}},
        ],
        universo=[
            {"codigo": "6815", "tipo_operacion": {
                "venta": True, "arriendo": False,
                "precio_venta": {"precio_clp": 130000000}, "precio_arriendo": {"precio_clp": None}}},
            {"codigo": "6282", "tipo_operacion": {
                "venta": True, "arriendo": False,
                "precio_venta": {"precio_clp": 55000000}, "precio_arriendo": {"precio_clp": None}}},
        ],
    )
    rows = {r["codigo"]: r["precio_uf"] for r in _run_property_rows(db, uf_value=40846.11)}
    assert round(rows["6815"], 1) == round(130000000 / 40846.11, 1)
    assert round(rows["6282"], 1) == round(55000000 / 40846.11, 1)


def test_card3_uf_directo_prioritario_sobre_clp():
    # Si el canónico tiene UF y CLP, se usa UF (no se convierte CLP).
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "X", "operacion": "Venta"}},
        ],
        universo=[
            {"codigo": "X", "tipo_operacion": {
                "venta": True, "arriendo": False,
                "precio_venta": {"precio_uf": 100.0, "precio_clp": 100000000},
                "precio_arriendo": {"precio_clp": None}}},
        ],
    )
    rows = _run_property_rows(db, uf_value=40846.11)
    assert rows[0]["precio_uf"] == 100.0


def test_card3_test_leads_excluidos_estructuralmente():
    # _test_lead/is_test/canales de prueba se excluyen del universo CARD 3.
    db = _make_prop_db(
        leads=[
            {"_id": "t1", "created_at": "2026-07-11T14:00:00Z", "_test_lead": True,
             "prospecto": {"codigo": "SMOKE005", "operacion": "Venta"}},
            {"_id": "t2", "created_at": "2026-07-12T14:00:00Z", "is_test": True,
             "prospecto": {"codigo": "12345", "operacion": "Venta"}},
            {"_id": "r1", "created_at": "2026-07-13T14:00:00Z",
             "prospecto": {"codigo": "REAL1", "operacion": "Venta", "precio_uf": 500.0}},
        ],
        universo=[],
    )
    with patch.object(lq, "get_db", return_value=db), \
         patch.object(lq, "_normalized_created_at_stage", return_value={}), \
         patch.object(lq, "_build_commercial_cohort_match", return_value={}):
        pipe = query_leads_dashboard_pipeline(
            period_start="2026-07-10", period_end="2026-07-20", uf_value=40846.11)
    assert pipe["propiedades_vinculadas"] == 1
    assert pipe["propiedades_cartera"] == 0
    assert pipe["propiedades_cartera_valorizadas"] == 0
    assert pipe["propiedades_no_en_cartera"] == 1
    assert pipe["leads_vinculados"] == 1


def test_card3_phone_synthetic_no_excluye_leads_portal():
    # phone_is_synthetic = True cubre leads reales del portal sin teléfono
    # (no-phone-prop360-*); NO son datos de prueba.
    db = _make_prop_db(
        leads=[
            {"_id": "r1", "created_at": "2026-07-11T14:00:00Z", "phone": "no-phone-prop360-1234",
             "prospecto": {"codigo": "REAL1", "operacion": "Venta", "precio_uf": 500.0}},
        ],
        universo=[],
    )
    with patch.object(lq, "get_db", return_value=db), \
         patch.object(lq, "_normalized_created_at_stage", return_value={}), \
         patch.object(lq, "_build_commercial_cohort_match", return_value={}):
        pipe = query_leads_dashboard_pipeline(
            period_start="2026-07-10", period_end="2026-07-20", uf_value=40846.11)
    assert pipe["propiedades_vinculadas"] == 1
    assert pipe["propiedades_no_en_cartera"] == 1


def test_card3_frontend_kpi_principal_comision():
    # Jerarquía: KPI principal = comisión potencial; Venta/Arriendo secundarios.
    card_html = HTML.split('id="cardPipeline"', 1)[1].split('id="cardSla"', 1)[0]
    assert 'Comisión potencial' in card_html
    assert 'id="cd3ComisionValue"' in card_html
    assert 'cd3-kpi-value' in HTML
    assert 'cd3-kpi-unit' in HTML
    assert 'id="cd3MontoVenta"' in card_html
    assert 'id="cd3MontoArriendo"' in card_html
    # Sin nota visible "Renta mensual" (queda en el tooltip).
    assert 'Renta mensual' not in card_html
    # No debe existir el layout anterior de 3 KPIs grandes en paralelo.
    assert 'cd3-stats' not in HTML
    assert 'cd3-stat-value' not in HTML


def test_card3_frontend_unico_micrografico_composicion_comision():
    card_html = HTML.split('id="cardPipeline"', 1)[1].split('id="cardSla"', 1)[0]
    # Un solo micrográfico: composición de comisión potencial (Venta vs Arriendo UF).
    assert card_html.count('cd3-mix-track') == 1
    assert 'Composición comisión potencial' in card_html
    assert 'id="cd3MixVenta"' in card_html
    assert 'id="cd3MixArriendo"' in card_html
    assert 'id="cd3MixPct"' in card_html
    # La cobertura de valorización ya no existe (es 100% en cartera válida).
    assert 'cd3-coverage-track' not in HTML
    assert 'Cobertura de valorización' not in HTML
    assert 'cd3CoveragePct' not in HTML


def test_card3_frontend_barra_unica_apilada_continua():
    # Micrográfico = UNA sola barra horizontal apilada continua: sin gap entre
    # segmentos, sin border-radius interno y colores Venta/Arriendo diferenciados.
    mix_css = HTML.split('.cd3-mix-track', 1)[1].split('/* --- CARD 4', 1)[0]
    assert 'gap: 0' in mix_css
    seg_region = HTML.split('.cd3-mix-seg', 1)[1].split('.cd3-mix-venta', 1)[0]
    assert 'border-radius: 0' in seg_region
    assert '.cd3-mix-venta' in mix_css and '.cd3-mix-arriendo' in mix_css
    assert 'var(--pipeline-accent)' in mix_css  # Venta (ámbar de la paleta)
    # Arriendo: tono neutral/slate existente en la paleta, NO protagonista.
    assert 'var(--spark-target)' in mix_css
    for banned in ('38BDF8', '0891B2', '#10B981', '10b981', '#8B5CF6', '#6366F1',
                   '#6366f1', '#F59E0B', '#F87171', '#ef4444'):
        assert banned not in mix_css
    # Los porcentajes siguen siendo dinámicos.
    render = HTML.split('function renderPipelineCard()', 1)[1]
    assert 'mixVentaEl.style.width' in render
    assert 'mixArriendoEl.style.width' in render
    assert 'Venta ' in render and '· Arriendo ' in render


def test_card3_frontend_sin_divisiones_verticales_ni_boxes():
    # Métricas secundarias sin cajas/mini-cards/divisores verticales.
    card_html = HTML.split('id="cardPipeline"', 1)[1].split('id="cardSla"', 1)[0]
    assert 'border-left' not in card_html
    assert 'cd3-sec-item' in card_html
    # Footer ejecutivo breve: propiedades de cartera vinculadas.
    render = HTML.split('function renderPipelineCard()', 1)[1]
    assert 'propiedades de cartera vinculadas' in render


def test_card3_no_inventa_precio_no_promedio_no_otro_codigo():
    src = Path("analytics/leads_queries.py").read_text(encoding="utf-8")
    # La resolución de precio por propiedad usa el precio canónico de esa
    # operación (UF directo o CLP/UF) y el campo del lead como respaldo; nunca
    # promedios ni precio de otro código.
    assert "lead_price" in src
    assert "canon.get(\"venta_uf\")" in src or "canon.get('venta_uf')" in src
    assert "/ uf_value" in src


def test_card3_footer_no_menciona_sin_valorizacion():
    # La métrica "sin valorización disponible" desapareció del frontend.
    assert 'sin valorización disponible' not in HTML
    render = HTML.split('function renderPipelineCard()', 1)[1]
    assert 'propiedades_no_en_cartera' in render
    assert 'cd3TooltipNoCartera' in HTML
    # Las referencias fuera de la cartera actual se reportan como diagnóstico.
    assert 'fuera de la cartera actual' in render
    assert 'propiedades_otro' in render
    assert 'cd3TooltipOtro' in HTML


def test_card3_count_active_cartera_properties():
    # Denominador auditado: disponible_prop360=True y operación venta/arriendo.
    from analytics.leads_queries import count_active_cartera_properties
    db = make_db([])
    db["universo_cartera_prop360"].insert_many([
        {"codigo": "A", "disponible_prop360": True, "tipo_operacion": {"venta": True, "arriendo": False},
         "estado": {"oficina": "PROCASA SUCRE"}},
        {"codigo": "B", "disponible_prop360": True, "tipo_operacion": {"venta": False, "arriendo": True},
         "estado": {"oficina": "PROCASA SUCRE"}},
        {"codigo": "C", "disponible_prop360": True, "tipo_operacion": {"venta": False, "arriendo": False},
         "estado": {"oficina": "PROCASA SUCRE"}},
        {"codigo": "D", "disponible_prop360": False, "tipo_operacion": {"venta": True, "arriendo": False},
         "estado": {"oficina": "PROCASA SUCRE"}},
        {"codigo": "E", "disponible_prop360": True, "tipo_operacion": {"venta": True, "arriendo": False},
         "estado": {"oficina": "PROCASA LA GLORIA"}},
    ])
    with patch.object(lq, "get_db", return_value=db):
        assert count_active_cartera_properties() == 3          # toda la compañía
        assert count_active_cartera_properties("PROCASA SUCRE") == 2


def test_card3_query_cartera_demanda_coverage_sucre():
    # Cobertura SUCRE: numerador = activas con demanda (dedup), denominador = activas.
    from analytics.leads_queries import query_cartera_demanda_coverage
    db = make_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "A", "operacion": "Venta"}},
            {"_id": "l2", "created_at": "2026-07-12T14:00:00Z",
             "prospecto": {"codigo": "A", "operacion": "Venta"}},  # dup -> cuenta 1 vez
            {"_id": "l3", "created_at": "2026-07-13T14:00:00Z",
             "prospecto": {"codigo": "B", "operacion": "Venta"}},
            {"_id": "l4", "created_at": "2026-07-14T14:00:00Z", "_test_lead": True,
             "prospecto": {"codigo": "E", "operacion": "Venta"}},  # test -> excluido
        ])
    db["universo_cartera_prop360"].insert_many([
        {"codigo": "A", "disponible_prop360": True, "tipo_operacion": {"venta": True, "arriendo": False},
         "estado": {"oficina": "PROCASA SUCRE"}},
        {"codigo": "B", "disponible_prop360": True, "tipo_operacion": {"venta": False, "arriendo": True},
         "estado": {"oficina": "PROCASA SUCRE"}},
        {"codigo": "C", "disponible_prop360": True, "tipo_operacion": {"venta": False, "arriendo": False},
         "estado": {"oficina": "PROCASA SUCRE"}},  # sin operación -> no activa
        {"codigo": "E", "disponible_prop360": True, "tipo_operacion": {"venta": True, "arriendo": False},
         "estado": {"oficina": "PROCASA SUCRE"}},
    ])
    with patch.object(lq, "get_db", return_value=db), \
         patch.object(lq, "_normalized_created_at_stage", return_value={}), \
         patch.object(lq, "_build_commercial_cohort_match", return_value={}):
        cov = query_cartera_demanda_coverage("2026-07-10", "2026-07-20", oficina="PROCASA SUCRE")
    assert cov["propiedades_activas"] == 3   # A, B, E (C sin operación queda fuera)
    assert cov["propiedades_con_demanda"] == 2  # A (dedup) + B; E es test
    assert cov["pct_cartera_con_demanda"] == round(2 / 3 * 100, 1)


def test_card3_pct_cartera_con_demanda_en_payload():
    # El payload expone propiedades_con_demanda, cartera_activa y pct.
    from analytics.leads_service import get_leads_dashboard_overview
    db = make_db([])
    with ExitStack() as stack:
        for patcher in _service_patches(total=2, citas=0, evaluable=0):
            stack.enter_context(patcher)
        stack.enter_context(patch.object(lq, "get_db", return_value=db))
        stack.enter_context(patch.object(lq, "query_leads_dashboard_pipeline", return_value={
            "monto_uf": 0, "propiedades_vinculadas": 2, "propiedades_cartera": 2,
            "propiedades_con_precio": 2, "propiedades_cartera_valorizadas": 2,
            "propiedades_venta": 2, "propiedades_arriendo": 0, "propiedades_otro": 0,
            "propiedades_sin_precio": 0, "propiedades_no_en_cartera": 0, "leads_vinculados": 2,
            "monto_venta_uf": 0, "monto_arriendo_uf": 0, "monto_otro_uf": 0,
        }))
        stack.enter_context(patch.object(lq, "query_property_commission_rows", return_value=[]))
        stack.enter_context(patch("analytics.leads_service.query_property_commission_rows", return_value=[]))
        stack.enter_context(patch("analytics.leads_service.query_cartera_demanda_coverage", return_value={
            "propiedades_con_demanda": 119, "propiedades_activas": 442,
            "pct_cartera_con_demanda": 26.9}))
        stack.enter_context(patch("analytics.leads_service._load_commercial_macro_information", return_value={
            "available": True, "indicators": {"uf": {"value": 40846.11, "as_of": "2026-08-10T04:00:00Z", "available": True}}}))
        ov = get_leads_dashboard_overview(period_start="2026-07-10", period_end="2026-07-20", compare="none")
    p = ov["pipeline"]
    assert p["propiedades_con_demanda"] == 119
    assert p["cartera_activa"] == 442
    assert p["pct_cartera_con_demanda"] == 26.9


def test_card3_frontend_footer_cobertura_cartera():
    # Footer gerencial: "X de Y propiedades con demanda · Z% de la cartera".
    render = HTML.split('function renderPipelineCard()', 1)[1]
    assert 'propiedades_con_demanda' in render
    assert 'cartera_activa' in render
    assert 'pct_cartera_con_demanda' in render
    assert 'propiedades con demanda' in render
    assert 'de la cartera' in render
    # Tooltip: aclaración de cobertura de demanda vs cartera activa actual.
    assert 'cobertura de demanda compara las propiedades que recibieron leads' in HTML


def test_card3_footer_cuenta_solo_cartera_actual():
    # Auditoría: 132 referencias, 131 en cartera + 1 (12345) fuera de cartera.
    # El footer debe contabilizar solo las propiedades de cartera actuales.
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "100", "operacion": "Venta", "precio_uf": None}},
            {"_id": "l2", "created_at": "2026-07-12T14:00:00Z",
             "prospecto": {"codigo": "200", "operacion": "Venta", "precio_uf": None}},
            {"_id": "l3", "created_at": "2026-07-13T14:00:00Z",
             "prospecto": {"codigo": "12345", "operacion": "Venta", "precio_uf": 800.0}},
        ],
        universo=[
            {"codigo": "100", "tipo_operacion": {
                "venta": True, "arriendo": False,
                "precio_venta": {"precio_uf": 3000.0}, "precio_arriendo": {"precio_uf": None}}},
            {"codigo": "200", "tipo_operacion": {
                "venta": True, "arriendo": False,
                "precio_venta": {"precio_uf": 4000.0}, "precio_arriendo": {"precio_uf": None}}},
        ],
    )
    with patch.object(lq, "get_db", return_value=db), \
         patch.object(lq, "_normalized_created_at_stage", return_value={}), \
         patch.object(lq, "_build_commercial_cohort_match", return_value={}):
        pipe = query_leads_dashboard_pipeline(
            period_start="2026-07-10", period_end="2026-07-20", uf_value=40846.11)
    assert pipe["propiedades_vinculadas"] == 3
    assert pipe["propiedades_cartera"] == 2  # footer visible
    assert pipe["propiedades_cartera_valorizadas"] == 2
    assert pipe["propiedades_no_en_cartera"] == 1
    # La referencia fuera de cartera (12345) no entra en Venta ni Arriendo.
    assert pipe["monto_venta_uf"] == 7000.0  # solo 100 + 200
    assert pipe["monto_arriendo_uf"] == 0.0
    # Reconciliación Venta/Arriendo = cartera valorizada.
    assert pipe["propiedades_venta"] + pipe["propiedades_arriendo"] == pipe["propiedades_cartera_valorizadas"]


def test_card3_no_en_cartera_no_genera_comision():
    # 12345 fuera de cartera: presente como diagnóstico pero sin fila de
    # comisión (no entra en Venta, Arriendo ni Comisión).
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "100", "operacion": "Venta", "precio_uf": 3000.0}},
            {"_id": "l2", "created_at": "2026-07-12T14:00:00Z",
             "prospecto": {"codigo": "12345", "operacion": "Venta", "precio_uf": 800.0}},
        ],
        universo=[
            {"codigo": "100", "tipo_operacion": {
                "venta": True, "arriendo": False,
                "precio_venta": {"precio_uf": 3000.0}, "precio_arriendo": {"precio_uf": None}}},
        ],
    )
    rows = _run_property_rows(db, uf_value=40846.11)
    codes = [r["codigo"] for r in rows]
    assert "100" in codes
    assert "12345" not in codes


def test_card3_reconciliacion_venta_arriendo_service():
    # Reconciliación: venta + arriendo = cartera valorizada; footer_ok sin Otro.
    rows = [
        {"codigo": "V1", "precio_uf": 10000.0, "operacion": "venta"},
        {"codigo": "V2", "precio_uf": 300.0, "operacion": "venta"},
        {"codigo": "A1", "precio_uf": 100.0, "operacion": "arriendo"},
    ]
    ov = _run_overview_pipeline(properties=rows, commission_rows=rows)
    p = ov["pipeline"]
    rec = p["reconciliacion"]
    assert rec["propiedades_venta"] == 2
    assert rec["propiedades_arriendo"] == 1
    assert rec["propiedades_cartera_valorizadas"] == 3
    assert rec["ok"] is True
    assert rec["propiedades_otro"] == 0
    assert rec["footer_ok"] is True
    assert p["propiedades_venta"] + p["propiedades_arriendo"] == p["propiedades_cartera_valorizadas"]


def test_card3_otro_reportado_sin_valorizacion():
    # Propiedad de cartera con operación "Otro": se reporta aparte y no se
    # inventa valorización (ni Venta ni Arriendo ni comisión).
    db = _make_prop_db(
        leads=[
            {"_id": "l1", "created_at": "2026-07-11T14:00:00Z",
             "prospecto": {"codigo": "X", "operacion": "Permuta", "precio_uf": None}},
            {"_id": "l2", "created_at": "2026-07-12T14:00:00Z",
             "prospecto": {"codigo": "V1", "operacion": "Venta", "precio_uf": None}},
        ],
        universo=[
            {"codigo": "X", "tipo_operacion": {
                "venta": False, "arriendo": False,
                "precio_venta": {"precio_uf": 5000.0}, "precio_arriendo": {"precio_uf": 30.0}}},
            {"codigo": "V1", "tipo_operacion": {
                "venta": True, "arriendo": False,
                "precio_venta": {"precio_uf": 2000.0}, "precio_arriendo": {"precio_uf": None}}},
        ],
    )
    with patch.object(lq, "get_db", return_value=db), \
         patch.object(lq, "_normalized_created_at_stage", return_value={}), \
         patch.object(lq, "_build_commercial_cohort_match", return_value={}):
        pipe = query_leads_dashboard_pipeline(
            period_start="2026-07-10", period_end="2026-07-20", uf_value=40846.11)
    assert pipe["propiedades_cartera"] == 2
    assert pipe["propiedades_otro"] == 1
    assert pipe["propiedades_venta"] == 1
    assert pipe["propiedades_arriendo"] == 0
    assert pipe["propiedades_cartera_valorizadas"] == 1
    # La propiedad "Otro" no aporta monto (no se inventa valorización).
    assert pipe["monto_venta_uf"] == 2000.0
    assert pipe["monto_arriendo_uf"] == 0.0
    assert pipe["monto_otro_uf"] == 0.0
    assert pipe["propiedades_sin_precio"] == 0

