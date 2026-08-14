"""Pruebas definitivas de CARD 4 — SLA & VELOCIDAD DE RESPUESTA.

Cubre: jerarquía de evidencia HOT, as-of (sin look-ahead), universo por
categorías mutuamente excluyentes, KPI "En SLA al corte", velocidad por
perfil, exclusión de leads de prueba y diseño del frontend.
"""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from analytics.leads_queries import build_sla_risk_payload
from chatbot.constants import CHILE_TZ

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "leads_dashboard.html"
HTML = TEMPLATE.read_text(encoding="utf-8")


def cl(day, hour, minute=0):
    return CHILE_TZ.localize(datetime(2026, 7, day, hour, minute))


def mk(assigned, managed=None, hot_since=None, temp="COLD", last_intent=None,
       last_intent_at=None, alerts=None, history=None, test_lead=False):
    lc = {"assigned_at": assigned}
    if managed is not None:
        lc["first_valid_management_at"] = managed
    if hot_since is not None:
        lc["hot_since"] = hot_since
    lead = {
        "lifecycle": lc,
        "lead_temperature_effective": temp,
        "temperature_history": history or [],
        "last_intent": last_intent,
        "last_intent_at": last_intent_at,
        "prospecto": {"alerts_sent": alerts or {}},
    }
    if test_lead:
        lead["_test_lead"] = True
    return lead


def run(rows, now, cutoff=None, exclude_tests=True):
    return build_sla_risk_payload(
        rows, now=now, cutover_at="2026-01-01T00:00:00Z",
        cutoff_at=cutoff or now, exclude_tests=exclude_tests,
    )


# =============================================================================
# 1-3. HOT no reescribe el pasado
# =============================================================================

def test_hot_desde_antes_de_asignacion_sla_inicia_en_assigned():
    # HOT ya antes de asignar: sla_start = assigned_at (9:00). Gestion a las
    # 9:59 -> 59 min < 60 -> within HOT. (Si partiera de hot_since 8:00 serian
    # 119 min -> outside.)
    r = run([mk(cl(27, 9), managed=cl(27, 9, 59), hot_since=cl(27, 8), temp="HOT")], cl(27, 12))
    assert r["lead_hot"]["managed_within"] == 1
    assert r["lead_hot"]["eligible"] == 1
    assert r["lead"]["eligible"] == 0


def test_hot_despues_de_asignacion_antes_de_gestion_sla_inicia_en_hot_start():
    # assigned 9:00, hot_since 10:00, gestion 10:30 -> 30 min < 60 -> within.
    # Si partiera de assigned serian 90 min -> outside.
    r = run([mk(cl(27, 9), managed=cl(27, 10, 30), hot_since=cl(27, 10), temp="HOT")], cl(27, 12))
    assert r["lead_hot"]["managed_within"] == 1


def test_primera_gestion_antes_de_hot_start_se_evalua_normal():
    # managed 10:00 < hot_since 11:00 -> la primera gestion se resolvio Normal.
    r = run([mk(cl(27, 9), managed=cl(27, 10), hot_since=cl(27, 11), temp="HOT")], cl(27, 12))
    assert r["lead"]["managed_within"] == 1
    assert r["lead_hot"]["eligible"] == 0


# =============================================================================
# 4, 11. As-of: el HOT no reescribe el pasado ni el period_end
# =============================================================================

def test_hot_posterior_al_period_end_no_reescribe_historico():
    # cutoff 11:00; hot_since 12:00 (posterior) -> en el snapshot NO era HOT.
    r = run([mk(cl(27, 9), hot_since=cl(27, 12), temp="HOT")], cl(27, 12), cutoff=cl(27, 11))
    assert r["lead_hot"]["eligible"] == 0
    # Se evalua por su evidencia disponible hasta el corte: abierto en atencion.
    assert r["lead"]["attention"] == 1


def test_evidencia_exactamente_en_period_end_no_cuenta():
    # hot_since == cutoff 11:00 -> NO HOT; gestion == cutoff -> abierto al corte.
    r = run([mk(cl(27, 9), managed=cl(27, 11), hot_since=cl(27, 11), temp="HOT")], cl(27, 12), cutoff=cl(27, 11))
    assert r["lead_hot"]["eligible"] == 0
    assert r["lead"]["attention"] == 1  # abierto (gestion posterior al corte)
    assert r["managed"] == 0


# =============================================================================
# 5-8. Jerarquia de evidencia HOT
# =============================================================================

def test_lifecycle_hot_since_funciona_como_evidencia():
    r = run([mk(cl(27, 9), hot_since=cl(27, 10), temp="HOT")], cl(27, 12))
    assert r["lead_hot"]["eligible"] == 1
    assert r["excluded"]["hot_no_traceability"] == 0


def test_last_intent_at_funciona_como_fallback():
    r = run([mk(cl(27, 9), temp="HOT", last_intent="ASK_VISIT", last_intent_at=cl(27, 10))], cl(27, 12))
    assert r["lead_hot"]["eligible"] == 1


def test_alert_timestamp_funciona_como_fallback():
    r = run([mk(cl(27, 9), temp="HOT", alerts={"InteresVisita": cl(27, 10).isoformat()})], cl(27, 12))
    assert r["lead_hot"]["eligible"] == 1


def test_hot_sin_evidencia_es_no_evaluable_no_normal():
    r = run([mk(cl(27, 9), temp="HOT")], cl(27, 12))
    assert r["excluded"]["hot_no_traceability"] == 1
    assert r["lead"]["eligible"] == 0
    assert r["lead_hot"]["eligible"] == 0


# =============================================================================
# 9-10, 23. Look-ahead corregido
# =============================================================================

def test_gestion_posterior_al_cutoff_no_cuenta_como_gestionada():
    r = run([mk(cl(27, 9), managed=cl(27, 12))], cl(27, 12), cutoff=cl(27, 11))
    assert r["managed"] == 0
    assert r["lead"]["attention"] == 1  # abierto al corte (120 min)


def test_abierto_historico_se_evalua_hasta_period_end_no_hasta_hoy():
    # now = dia 30 (muy posterior); el reloj debe congelarse en cutoff 11:00.
    r = run([mk(cl(27, 9))], cl(30, 9), cutoff=cl(27, 11))
    assert r["lead"]["attention"] == 1  # 120 min al corte, no vencido
    assert r["lead"]["breached"] == 0


def test_historico_no_cambia_al_ejecutarse_dias_despues():
    rows = [
        mk(cl(27, 9), managed=cl(27, 10)),
        mk(cl(27, 9), managed=cl(27, 13)),
        mk(cl(27, 9)),
        mk(cl(27, 9), hot_since=cl(27, 9, 30), temp="HOT"),
    ]
    cutoff = cl(28, 9)
    later = CHILE_TZ.localize(datetime(2026, 8, 5, 12))
    a = run(rows, cl(28, 12), cutoff=cutoff)
    b = run(rows, later, cutoff=cutoff)
    assert a["overall_in_sla_pct"] == b["overall_in_sla_pct"]
    assert a["in_sla_count"] == b["in_sla_count"]
    assert a["out_sla_count"] == b["out_sla_count"]
    assert a["lead"]["breached"] == b["lead"]["breached"]


# =============================================================================
# 12. Leads de prueba
# =============================================================================

def test_test_lead_queda_excluido():
    r = run([mk(cl(27, 9), managed=cl(27, 10), test_lead=True)], cl(27, 12))
    assert r["excluded"]["excluded_tests"] == 1
    assert r["eligible_total"] == 0
    r2 = run([mk(cl(27, 9), managed=cl(27, 10), test_lead=True)], cl(27, 12), exclude_tests=False)
    assert r2["eligible_total"] == 1


# =============================================================================
# 13-15. Universo en SLA / fuera SLA / eligible
# =============================================================================

def test_in_sla_es_within_mas_abiertos_dentro():
    rows = [
        mk(cl(27, 9), managed=cl(27, 9, 30)),      # managed_within
        mk(cl(27, 9)),                              # open 15 min -> open_within (now 9:15)
    ]
    r = run(rows, cl(27, 9, 15))
    assert r["lead"]["in_sla_count"] == 2
    assert r["lead"]["out_sla_count"] == 0
    assert r["lead"]["eligible"] == 2


def test_out_sla_es_outside_mas_breached():
    rows = [
        mk(cl(27, 9), managed=cl(27, 13)),          # managed_outside (240)
        mk(cl(27, 9)),                              # open 240 -> breached (now 13:00)
    ]
    r = run(rows, cl(27, 13))
    assert r["lead"]["out_sla_count"] == 2
    assert r["lead"]["in_sla_count"] == 0
    assert r["lead"]["eligible"] == 2


def test_in_sla_mas_out_sla_igual_eligible():
    rows = [
        mk(cl(27, 9), managed=cl(27, 9, 30)),
        mk(cl(27, 9), managed=cl(27, 13)),
        mk(cl(27, 9)),
        mk(cl(27, 9), hot_since=cl(27, 9, 30), temp="HOT"),
    ]
    r = run(rows, cl(27, 13))
    for bucket in (r["lead"], r["lead_hot"]):
        assert bucket["in_sla_count"] + bucket["out_sla_count"] == bucket["eligible"]
    assert (r["lead"]["in_sla_count"] + r["lead_hot"]["in_sla_count"]
            + r["lead"]["out_sla_count"] + r["lead_hot"]["out_sla_count"]
            == r["eligible_total"])


# =============================================================================
# 16-17, 20. Velocidad por perfil y thresholds
# =============================================================================

def test_p50_hot_usa_solo_gestionados_hot():
    rows = [
        mk(cl(27, 9), managed=cl(27, 9, 30), hot_since=cl(27, 9), temp="HOT"),      # 30
        mk(cl(27, 9), managed=cl(27, 10, 30), hot_since=cl(27, 9, 30), temp="HOT"),  # 60
        mk(cl(27, 9), managed=cl(27, 10)),  # normal NO debe entrar
    ]
    r = run(rows, cl(27, 12))
    assert r["lead_hot"]["managed_sample"] == 2
    assert r["lead_hot"]["median_minutes"] == 45
    assert r["lead_hot"]["p50_minutes"] == r["lead_hot"]["median_minutes"]
    assert r["lead"]["managed_sample"] == 1


def test_p50_normal_usa_solo_gestionados_normal():
    rows = [
        mk(cl(27, 9), managed=cl(27, 10)),      # 60
        mk(cl(27, 9), managed=cl(27, 12)),      # 180
        mk(cl(27, 9), managed=cl(27, 9, 30), hot_since=cl(27, 9), temp="HOT"),  # HOT
    ]
    r = run(rows, cl(27, 13))
    assert r["lead"]["managed_sample"] == 2
    assert r["lead"]["median_minutes"] == 120
    assert r["lead_hot"]["managed_sample"] == 1


def test_thresholds_correctos_por_perfil():
    r = run([], cl(27, 12))
    assert r["lead"]["threshold_minutes"] == 180
    assert r["lead_hot"]["threshold_minutes"] == 60


# =============================================================================
# 22. JSON serializable
# =============================================================================

def test_payload_json_serializable():
    rows = [
        mk(cl(27, 9), managed=cl(27, 10)),
        mk(cl(27, 9), hot_since=cl(27, 9, 30), temp="HOT"),
    ]
    json.dumps(run(rows, cl(27, 12)))


# =============================================================================
# 18-21, 24. Frontend
# =============================================================================

def _render():
    return HTML.split("function renderSlaCard()", 1)[1].split("// --- EXPORTAR", 1)[0]


def test_frontend_no_mediana_agregada_como_kpi():
    render = _render()
    assert "in_sla_pct" in render
    assert "mediana_general_min" not in render
    assert "pct_cumplimiento_sla" not in render


def test_frontend_barras_representan_in_sla_pct():
    render = _render()
    assert "hotPct + '%'" in render
    assert "normPct + '%'" in render
    # Las barras ya no usan mediana/umbral.
    assert "cd4HotMin" not in render
    assert "cd4NormalMin" not in render


def test_frontend_hot_normal_con_thresholds():
    card_html = HTML.split('id="cardSla"', 1)[1].split('id="cd4Footer"', 1)[0]
    assert "HOT" in card_html and "NORMAL" in card_html
    render = _render()
    assert "Meta " in render and " min" in render
    assert "hot_threshold_min" in render and "normal_threshold_min" in render


def test_frontend_terminologia_primera_gestion_registrada():
    assert "primera respuesta efectiva" not in HTML
    assert "primera gestión registrada" in HTML


def test_frontend_visible_cambio_leads_dentro_de_sla():
    # "En SLA al corte" -> "Leads dentro de SLA"; "X% en SLA" -> "X% dentro de SLA".
    assert "Leads dentro de SLA" in HTML
    assert "En SLA al corte" not in HTML
    render = _render()
    assert "% dentro de SLA" in render
    assert "% en SLA" not in render


def test_frontend_tooltips_de_las_cuatro_cards():
    # Los 4 tooltips comparten la misma clase (mismo ancho/padding/tipografia).
    assert HTML.count('class="cd1-tooltip"') >= 4
    for tip_id in ("cd1Tooltip", "cd2Tooltip", "cd3Tooltip", "cd4Tooltip"):
        assert f'id="{tip_id}"' in HTML
    # CARD 4 debe tener handler de tooltip (hover/click) como las demas.
    assert "bindCardTooltip('cd4Info', 'cd4Tooltip')" in HTML
    assert "bindCardTooltip('cd1Info', 'cd1Tooltip')" in HTML
    # Sin nombres internos ni formulas tecnicas en los tooltips.
    assert "_scheduled_visit_lead_ids" not in HTML
    assert "universo_cartera_prop360" not in HTML


def test_frontend_cero_errores_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node no disponible")
    # Extraer cada bloque <script> y validar sintaxis por separado.
    import re
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", HTML, re.S)
    assert scripts, "no se encontraron scripts"
    for i, body in enumerate(scripts):
        if not body.strip():
            continue
        tmp = ROOT / f"tmp_js_check_{i}.js"
        tmp.write_text(body, encoding="utf-8")
        try:
            proc = subprocess.run([node, "--check", str(tmp)],
                                  capture_output=True, text=True, timeout=60)
            assert proc.returncode == 0, f"JS error en script {i}: {proc.stderr}"
        finally:
            tmp.unlink(missing_ok=True)
