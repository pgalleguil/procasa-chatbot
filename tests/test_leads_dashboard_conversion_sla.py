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
    assert "_estado" in SERVICE_SRC
    assert "SLA Crítico" not in SERVICE_SRC
    assert "Top Performer" not in SERVICE_SRC


def test_no_30_minute_threshold_in_frontend_resumen_ejecutivo():
    assert "sla < 30" not in HTML
    assert "sla<30" not in HTML
    assert "Verde: &lt; 30 min" not in HTML
    assert "umbral 30 min" not in HTML
    assert "≥ 30 min" not in HTML


def test_exec_table_sla_column_is_descriptive_not_classified():
    # The SLA column tooltip now explains the mixed-policy aggregate.
    assert "Hot 60 min y Normal 180 min" in HTML
    assert "no se clasifica con un umbral único" in HTML
    # The SLA cell no longer applies exec-sla-ok/bad coloring based on threshold.
    cell_tpl = HTML.split("const slaText = (sla === null")[1]
    assert "exec-sla-ok' : 'exec-sla-bad" not in cell_tpl


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
            "monto_uf": 0, "propiedades_vinculadas": 0, "leads_vinculados": 0,
            "monto_venta_uf": 0, "monto_arriendo_uf": 0, "monto_otro_uf": 0,
        }),
        patch.object(lq, "query_sla_risk_panel", return_value={
            "overall_median_minutes": None, "overall_compliance_pct": None,
            "lead_hot": {}, "lead": {}, "eligible_total": 0, "no_management": 0, "critical_open": 0,
        }),
        patch.object(lq, "query_leads_dashboard_rescue", return_value={"recuperabilidad_alta": 0}),
        patch.object(lq, "query_leads_dashboard_sources", return_value={"current": [], "previous": {}, "total": 0}),
        patch.object(lq, "query_leads_dashboard_executives", return_value=[]),
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
