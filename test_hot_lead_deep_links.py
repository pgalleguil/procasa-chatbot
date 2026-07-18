from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from jinja2 import Environment, FileSystemLoader

from chatbot.lead_router import (
    build_crm_lead_url,
    format_summary_whatsapp_template,
    format_whatsapp_template,
)
from chatbot.crm_updates import bump_crm_leads_version, get_crm_leads_version
from chatbot import storage
from chatbot.lead_temperature import COLD, HOT, derive_effective_temperature
from chatbot.crm_filters import build_crm_card_urls, build_crm_filter_urls
from api_crm import (
    CRM_COLD_QUERY,
    CRM_HOT_QUERY,
    crm_stage_group,
    normalize_crm_temperature,
)


def test_hot_lead_url_targets_specific_phone_and_property():
    url = build_crm_lead_url(
        {"phone": "+56 9 1234 5678", "property_code": "ABC 123"}
    )
    assert url.endswith("/crm/lead/56912345678?codigo=ABC+123")


def test_individual_hot_message_contains_direct_link_not_crm_root():
    message = format_whatsapp_template(
        {"phone": "+56912345678", "property_code": "7788", "last_message": "Quiero visitar"},
        "Erika Garrido",
        "7788",
    )
    assert "/crm/lead/56912345678?codigo=7788" in message
    assert "Ver y Gestionar en CRM" in message
    assert "onrender.com/\n\n" not in message


def test_summary_contains_one_direct_link_per_hot_lead():
    message = format_summary_whatsapp_template(
        [
            {"lead_data": {"phone": "+56911111111", "property_code": "100"}},
            {"lead_data": {"phone": "+56922222222", "property_code": "200"}},
        ],
        "Erika Garrido",
    )
    assert "/crm/lead/56911111111?codigo=100" in message
    assert "/crm/lead/56922222222?codigo=200" in message
    assert "/crm?temperatura=HOT" in message


def test_missing_phone_falls_back_to_hot_queue_not_generic_home():
    url = build_crm_lead_url({"phone": "", "property_code": "100"})
    assert url.endswith("/crm?temperatura=HOT")


def test_summary_deduplicates_same_contact_and_property_across_alert_types():
    duplicate_lead = {
        "phone": "+56 9 5617 0838",
        "property_code": "5949",
        "nombre": "Cliente",
    }
    message = format_summary_whatsapp_template(
        [
            {"_id": "first", "lead_data": {**duplicate_lead, "lead_type": "InteresVisita"}},
            {"_id": "second", "lead_data": {**duplicate_lead, "lead_type": "LeadHotWhatsapp"}},
            {"_id": "third", "lead_data": {"phone": "+56989168767", "property_code": "7726"}},
            {"_id": "fourth", "lead_data": {"phone": "569 8916 8767", "property_code": "7.726"}},
        ],
        "Mariela Arriagada",
    )

    assert "2 Nuevos Leads Asignados" in message
    assert "tienes 2 nuevos leads" in message
    assert message.count("/crm/lead/56956170838?codigo=5949") == 1
    assert message.count("/crm/lead/56989168767?codigo=7726") == 1


def test_summary_keeps_two_properties_for_the_same_contact():
    message = format_summary_whatsapp_template(
        [
            {"lead_data": {"phone": "+56911111111", "property_code": "100"}},
            {"lead_data": {"phone": "+56911111111", "property_code": "200"}},
        ],
        "Erika Garrido",
    )

    assert "2 Nuevos Leads Asignados" in message
    assert "codigo=100" in message
    assert "codigo=200" in message


def test_existing_auth_flow_preserves_requested_lead_path():
    source = Path("webhook.py").read_text(encoding="utf-8")
    assert 'requested_url = request.url.path' in source
    assert 'response.set_cookie("login_next", requested_url' in source
    assert 'target_url = _safe_login_next(request.cookies.get("login_next"))' in source


def test_crm_pagination_uses_real_pages_and_preserves_filters():
    template = Path("templates/crm_leads_list.html").read_text(encoding="utf-8")
    webhook = Path("webhook.py").read_text(encoding="utf-8")
    api_crm = Path("api_crm.py").read_text(encoding="utf-8")
    pagination_start = template.index("<!-- PAGINACI")
    pagination_end = template.index("<!-- Overlay", pagination_start)
    pagination_markup = template[pagination_start:pagination_end]

    assert 'href="{{ pagination_base_url }}page={{ pagination.current_page - 1 }}"' in pagination_markup
    assert 'href="{{ pagination_base_url }}page={{ pagination.current_page + 1 }}"' in pagination_markup
    assert "cursor=" not in pagination_markup
    assert '"temperatura": temperatura' in webhook
    assert '"ejecutivo": ejecutivo' in webhook
    assert '"busqueda": busqueda' in webhook
    assert '"orden": orden' in webhook
    assert 'page=page' in webhook
    assert 'offset = (page - 1) * limit' in api_crm
    assert '{"$skip": offset}' in api_crm


def test_legacy_serialized_commercial_alert_is_normalized_once_for_backfill():
    lead = {
        "lead_temperature": "COLD",
        "prospecto": {
            "alerts_sent": '{"LeadHotWhatsapp": {"sent_at": "2026-07-01"}}'
        },
    }

    assert derive_effective_temperature(lead) == HOT
    assert CRM_HOT_QUERY == {"lead_temperature_effective": HOT}
    assert CRM_COLD_QUERY == {"lead_temperature_effective": COLD}
    template = Path("templates/crm_leads_list.html").read_text(encoding="utf-8")
    assert "lead.lead_temperature_effective == 'HOT'" in template
    assert "is_crm_hot_signal" not in Path("api_crm.py").read_text(encoding="utf-8")


def test_effective_hot_and_cold_are_exclusive_and_cover_classifiable_total():
    leads = [
        {"lead_temperature_effective": HOT},
        {"lead_temperature_effective": COLD},
        {"lead_temperature_effective": HOT},
        {"lead_temperature_effective": COLD},
    ]
    hot_ids = {index for index, lead in enumerate(leads) if lead["lead_temperature_effective"] == HOT}
    cold_ids = {index for index, lead in enumerate(leads) if lead["lead_temperature_effective"] == COLD}

    assert hot_ids.isdisjoint(cold_ids)
    assert len(hot_ids) + len(cold_ids) == len(leads)


def test_crm_stage_groups_partition_the_selected_temperature_total():
    stages = ["NEW"] * 37 + ["CONTACTED"] * 60 + ["INTERESTED"] * 12 + ["VISIT_DONE"] * 0 + ["CLOSED_WON"] * 6
    counts = {group: 0 for group in ("NEW", "GESTION", "VISITA", "CERRADO")}
    for stage in stages:
        counts[crm_stage_group(stage)] += 1

    assert counts == {"NEW": 37, "GESTION": 72, "VISITA": 0, "CERRADO": 6}
    assert sum(counts.values()) == len(stages) == 115


def test_each_temperature_scope_partitions_into_the_four_card_states():
    leads = (
        [{"temperature": HOT, "stage": "NEW"}] * 2
        + [{"temperature": HOT, "stage": "CONTACTED"}] * 80
        + [{"temperature": HOT, "stage": "CLOSED_WON"}] * 5
        + [{"temperature": COLD, "stage": "NEW"}] * 36
        + [{"temperature": COLD, "stage": "CONTACTED"}] * 66
        + [{"temperature": COLD, "stage": "CLOSED_LOST"}] * 4
    )

    for temperature, expected_total in (("Todos", 193), (HOT, 87), (COLD, 106)):
        scope = leads if temperature == "Todos" else [
            lead for lead in leads if lead["temperature"] == temperature
        ]
        counts = {group: 0 for group in ("NEW", "GESTION", "VISITA", "CERRADO")}
        for lead in scope:
            counts[crm_stage_group(lead["stage"])] += 1

        assert len(scope) == expected_total
        assert sum(counts.values()) == expected_total
        assert counts["GESTION"] + counts["VISITA"] + counts["CERRADO"] == expected_total - counts["NEW"]
        percentages = [count * 100 / expected_total for count in counts.values()]
        assert abs(sum(percentages) - 100.0) < 0.001

    for group in ("NEW", "GESTION", "VISITA", "CERRADO"):
        total_group = sum(crm_stage_group(lead["stage"]) == group for lead in leads)
        hot_group = sum(
            lead["temperature"] == HOT and crm_stage_group(lead["stage"]) == group
            for lead in leads
        )
        cold_group = sum(
            lead["temperature"] == COLD and crm_stage_group(lead["stage"]) == group
            for lead in leads
        )
        assert hot_group + cold_group == total_group


def test_compact_state_counts_and_percentages_share_the_same_source_values():
    total = 169
    counts = {"NEW": 81, "GESTION": 88, "VISITA": 0, "CERRADO": 0}
    percentages = {key: count * 100 / total for key, count in counts.items()}

    assert sum(counts.values()) == total
    assert round(sum(percentages.values()), 10) == 100.0
    assert counts["GESTION"] + counts["VISITA"] + counts["CERRADO"] == 88
    assert round(percentages["NEW"], 1) == 47.9
    assert round(percentages["GESTION"], 1) == 52.1


def test_each_state_card_url_filters_exactly_its_count_in_active_temperature():
    leads = (
        [{"temperature": HOT, "stage": "NEW"}] * 2
        + [{"temperature": HOT, "stage": "CONTACTED"}] * 80
        + [{"temperature": HOT, "stage": "CLOSED_WON"}] * 5
        + [{"temperature": COLD, "stage": "NEW"}] * 36
        + [{"temperature": COLD, "stage": "CONTACTED"}] * 66
        + [{"temperature": COLD, "stage": "CLOSED_LOST"}] * 4
    )
    state_to_group = {
        "new": "NEW",
        "grupo_gestion": "GESTION",
        "grupo_visita": "VISITA",
        "grupo_cerrado": "CERRADO",
    }

    for temperature in ("Todos", HOT, COLD):
        params = {} if temperature == "Todos" else {"temperatura": temperature}
        urls = build_crm_card_urls(params)
        scope = leads if temperature == "Todos" else [
            lead for lead in leads if lead["temperature"] == temperature
        ]
        for card_key, group in state_to_group.items():
            query = parse_qs(urlsplit(urls[card_key]).query)
            assert query.get("temperatura", ["Todos"])[0] == temperature
            assert query["estado"][0].lower() == card_key
            filtered = [lead for lead in scope if crm_stage_group(lead["stage"]) == group]
            counted = sum(crm_stage_group(lead["stage"]) == group for lead in scope)
            assert len(filtered) == counted


def test_crm_temperature_is_normalized_before_query_and_render():
    assert normalize_crm_temperature("hot") == HOT
    assert normalize_crm_temperature(" COLD ") == COLD
    assert normalize_crm_temperature(None) == "Todos"
    assert normalize_crm_temperature("invalid") == "Todos"


def test_crm_kpi_cards_filter_temperature_and_exact_stage_groups():
    template = Path("templates/crm_leads_list.html").read_text(encoding="utf-8")
    api_crm = Path("api_crm.py").read_text(encoding="utf-8")

    assert "card_urls.total" in template
    assert "card_urls.hot" in template
    assert "card_urls.cold" in template
    assert "card_urls.unassigned" in template
    for state in ("NEW", "GRUPO_GESTION", "GRUPO_VISITA", "GRUPO_CERRADO"):
        assert f"'{state}'" in template
    assert "kpis.managed_percent" in template
    assert "kpis.scope_total" in template
    assert "{{ management_title }}" in template
    assert "con gestión iniciada" not in template
    assert 'class="segmented-progress"' in template
    assert 'class="state-metrics-grid"' in template
    assert 'class="operational-alerts"' in template
    assert "kpis.sin_asignar_global" in template
    assert 'f"{state_key}_{temperature_key}"' in api_crm
    assert '"scope_total"' in api_crm
    assert 'filtro_estado == "GRUPO_GESTION"' in api_crm


def test_crm_temperature_cards_avoid_ambiguous_ratios_and_mobile_layout_is_ordered():
    template = Path("templates/crm_leads_list.html").read_text(encoding="utf-8")

    assert "{{ kpis.hot }} / {{ kpis.total }}" not in template
    assert "{{ kpis.cold }} / {{ kpis.total }}" not in template
    assert "{{ pct(kpis.hot_percent) }}% del total" in template
    assert "{{ pct(kpis.cold_percent) }}% del total" in template
    assert "state-cards-grid" not in template
    assert "state-card-ratio" not in template
    assert 'row-cols-md-3 temperature-cards' in template
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in template
    assert '.filter-bar select,' in template
    assert 'style="margin-left: auto;" name="orden"' not in template
    assert "grid-template-columns: minmax(92px, 34%) minmax(0, 1fr);" in template
    assert template.count('class="mobile-cell-value') == 8
    assert ".crm-table .mobile-cell-value" in template
    assert template.count("Seleccionado</span>") == 3
    assert 'aria-current="{{' in template
    assert "outline: 2px solid var(--accent-color);" in template
    assert "border-bottom: 3px solid var(--led-blue" not in template
    assert "document.querySelectorAll('#crmDynamicContent [data-target]')" in template
    assert "#crmDynamicContent .summary-value" not in template
    assert "if (!counter.classList.contains('summary-value'))" in template
    assert "counter.innerText = target;" in template
    assert ".segment-nuevo { background: #f87171; }" in template
    assert ".segment-gestion { background: #fbbf24; }" in template
    assert ".segment-visita { background: #34d399; }" in template
    assert ".segment-cerrado { background: #64748b; }" in template
    for segment_key in ("nuevo", "gestion", "visita", "cerrado"):
        assert f".segment-{segment_key} {{" in template


def test_crm_card_urls_preserve_executive_search_order_and_toggle_filters():
    urls = build_crm_card_urls({
        "temperatura": "COLD",
        "estado": "GRUPO_GESTION",
        "ejecutivo": "Mariela Arriagada",
        "busqueda": "569 123",
        "orden": "fecha",
        "page": "3",
    })

    def query(key):
        return parse_qs(urlsplit(urls[key]).query)

    for key in urls:
        params = query(key)
        assert params["ejecutivo"] == ["Mariela Arriagada"]
        assert params["busqueda"] == ["569 123"]
        assert params["orden"] == ["fecha"]
        assert params["page"] == ["1"]

    assert "temperatura" not in query("total") and "estado" not in query("total")
    assert query("hot")["temperatura"] == ["HOT"] and "estado" not in query("hot")
    assert "temperatura" not in query("cold") and "estado" not in query("cold")
    assert query("new")["temperatura"] == ["COLD"]
    assert query("new")["estado"] == ["NEW"]
    assert query("grupo_gestion")["temperatura"] == ["COLD"]
    assert "estado" not in query("grupo_gestion")
    assert "temperatura" not in query("unassigned")
    assert query("unassigned")["estado"] == ["UNASSIGNED"]
    assert query("new_hot")["temperatura"] == ["HOT"]
    assert query("new_hot")["estado"] == ["NEW"]
    assert query("new_cold")["temperatura"] == ["COLD"]
    assert query("new_cold")["estado"] == ["NEW"]


class _FakeRuntimeCollection:
    def __init__(self):
        self.document = None

    def find_one_and_update(self, query, update, **kwargs):
        current = dict(self.document or {"_id": query["_id"], "version": 0})
        current["version"] += update["$inc"]["version"]
        current.update(update["$set"])
        self.document = current
        return dict(current)

    def find_one(self, query, projection=None):
        return dict(self.document) if self.document else None


class _FakeLeadsCollection:
    def __init__(self):
        self.last_update = None

    def update_one(self, query, update, **kwargs):
        self.last_update = update
        return SimpleNamespace(modified_count=1, upserted_id=None)


def test_crm_update_version_is_atomic_and_monotonic():
    collection = _FakeRuntimeCollection()
    fake_db = {"crm_runtime_state": collection}

    assert bump_crm_leads_version(fake_db, "message_user", "56911111111") == 1
    assert bump_crm_leads_version(fake_db, "status_change", "56911111111") == 2
    assert get_crm_leads_version(fake_db) == 2
    assert collection.document["reason"] == "status_change"


def test_saving_message_records_visible_activity_and_bumps_version(monkeypatch):
    runtime = _FakeRuntimeCollection()
    leads = _FakeLeadsCollection()
    fake_db = {"crm_runtime_state": runtime, "leads": leads}
    monkeypatch.setattr(storage, "get_db", lambda: fake_db)

    storage.guardar_mensaje("56911111111", "user", "Quiero agendar una visita")

    assert leads.last_update["$set"]["last_message_role"] == "user"
    assert leads.last_update["$set"]["last_message_preview"] == "Quiero agendar una visita"
    assert runtime.document["version"] == 1
    assert runtime.document["reason"] == "message_user"


def test_crm_partial_template_contains_only_dynamic_regions():
    environment = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    template = environment.get_template("crm_leads_list.html")
    request = SimpleNamespace(query_params={"temperatura": "COLD", "ejecutivo": "Mariela Arriagada"})
    kpis = SimpleNamespace(
        total=3,
        scope_total=2,
        hot=1,
        cold=2,
        sin_asignar=0,
        sin_asignar_global=0,
        nuevo=1,
        gestion=1,
        visita=0,
        cerrado=0,
        managed=1,
        managed_percent=50.0,
        hot_percent=33.3,
        cold_percent=66.7,
        sin_asignar_percent=0.0,
        nuevo_percent=50.0,
        gestion_percent=50.0,
        visita_percent=0.0,
        cerrado_percent=0.0,
        nuevo_hot=0,
        nuevo_cold=1,
        gestion_hot=0,
        gestion_cold=1,
        visita_hot=0,
        visita_cold=0,
        cerrado_hot=0,
        cerrado_cold=0,
    )

    rendered = template.render(
        partial=True,
        request=request,
        leads=[],
        kpis=kpis,
        user_role="supervisor",
        user_name="Supervisor",
        executives=["Mariela Arriagada"],
        current_ejecutivo="Mariela Arriagada",
        current_temperatura="COLD",
        crm_version=7,
        card_urls=build_crm_card_urls(request.query_params),
        filter_urls=build_crm_filter_urls(request.query_params),
        pagination_base_url="/crm?temperatura=COLD&ejecutivo=Mariela+Arriagada&",
        pagination={
            "total_count": 0,
            "current_page": 1,
            "total_pages": 1,
            "has_prev": False,
            "has_more": False,
        },
    )

    assert 'id="crmDynamicContent"' in rendered
    assert 'data-crm-version="7"' in rendered
    assert "33,3% del total" in rendered
    assert "66,7% del total" in rendered
    assert "/ 2" in rendered
    assert "Sin atender" in rendered
    assert "Gestión de Leads informativos" in rendered
    assert "1 gestionados · 1 sin atender" in rendered
    assert "Gestionados = En gestión + Visitas + Cerrados" in rendered
    assert rendered.count('class="progress-segment ') == 2
    assert rendered.count('style="width: 50.0%;"') == 2
    assert 'class="progress-segment segment-visita"' not in rendered
    assert 'class="progress-segment segment-cerrado"' not in rendered
    assert "state-breakdown" not in rendered
    assert rendered.count('aria-current="true"') == 1
    assert "state-cards-grid" not in rendered
    assert "<html" not in rendered
    assert "sidebar" not in rendered


def test_total_state_temperature_breakdown_links_are_not_empty():
    environment = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    template = environment.get_template("crm_leads_list.html")
    request = SimpleNamespace(query_params={})
    kpis = SimpleNamespace(
        total=3, scope_total=3, hot=1, cold=2, sin_asignar=0,
        sin_asignar_global=0, nuevo=2, gestion=1, visita=0, cerrado=0,
        managed=1, managed_percent=33.3, hot_percent=33.3, cold_percent=66.7,
        sin_asignar_percent=0.0, nuevo_percent=66.7, gestion_percent=33.3,
        visita_percent=0.0, cerrado_percent=0.0,
        nuevo_hot=1, nuevo_cold=1, gestion_hot=0, gestion_cold=1,
        visita_hot=0, visita_cold=0, cerrado_hot=0, cerrado_cold=0,
    )
    rendered = template.render(
        partial=True, request=request, leads=[], kpis=kpis,
        user_role="supervisor", user_name="Supervisor", executives=[],
        current_ejecutivo="Todos", current_temperatura="Todos", crm_version=1,
        card_urls=build_crm_card_urls(request.query_params),
        filter_urls=build_crm_filter_urls(request.query_params),
        pagination_base_url="/crm?", pagination={"total_count": 3, "current_page": 1,
        "total_pages": 1, "has_prev": False, "has_more": False},
    )

    assert 'href="/crm?temperatura=HOT&amp;estado=NEW&amp;page=1">1 Hot' in rendered
    assert 'href="/crm?temperatura=COLD&amp;estado=NEW&amp;page=1">1 Informativos' in rendered


def test_crm_filter_urls_remove_one_filter_and_reset_page():
    urls = build_crm_filter_urls({
        "temperatura": "COLD",
        "estado": "NEW",
        "ejecutivo": "Erika Garrido",
        "busqueda": "5691",
        "orden": "sla_por_vencer",
        "page": "4",
    })

    temperature_query = parse_qs(urlsplit(urls["temperature"]).query)
    state_query = parse_qs(urlsplit(urls["state"]).query)
    assert "temperatura" not in temperature_query
    assert temperature_query["estado"] == ["NEW"]
    assert "estado" not in state_query
    assert state_query["temperatura"] == ["COLD"]
    for key in ("temperature", "state", "executive", "search", "order"):
        assert parse_qs(urlsplit(urls[key]).query)["page"] == ["1"]


def test_crm_filters_and_contextual_columns_are_wired_to_one_url_state():
    template = Path("templates/crm_leads_list.html").read_text(encoding="utf-8")
    api_crm = Path("api_crm.py").read_text(encoding="utf-8")

    assert 'id="crmFilterForm"' in template
    assert template.count("data-auto-filter") >= 4
    assert "setTimeout(() => applyCrmFilters(event.target.form), 500)" in template
    assert "params.delete('page')" in template
    assert "Limpiar filtros" in template
    assert 'class="filter-chip"' in template
    assert "leads encontrados" in template
    assert "show_priority = selected_temperature == 'Todos'" in template
    assert "show_executive = current_ejecutivo == 'Todos'" in template
    assert "Sin gestión registrada" in api_crm
    assert 'type_labels.get(last_ev.get("type"), "Acción registrada")' not in api_crm
    for order in (
        "antiguos_sin_atender",
        "sla_por_vencer",
        "mayor_sin_gestion",
        "ultima_accion_antigua",
    ):
        assert order in template
        assert order in api_crm


def test_crm_hybrid_polling_uses_partial_fetch_without_full_reload():
    template = Path("templates/crm_leads_list.html").read_text(encoding="utf-8")
    webhook = Path("webhook.py").read_text(encoding="utf-8")
    check_endpoint = webhook[webhook.index('async def check_crm_updates'):webhook.index('@app.get("/crm/partial"')]

    assert "const CRM_POLL_INTERVAL_MS = 20000" in template
    assert "document.hidden" in template
    assert "visibilitychange" in template
    assert "/crm/check-updates?since=" in template
    assert "/crm/partial${window.location.search}" in template
    assert "Hay nuevos cambios" in template
    assert "crm:mutation-complete" in template
    assert "refreshCrmList({ force: true })" in template
    assert "window.location.reload()" not in template
    assert '"/crm/check-updates"' in webhook
    assert '"/crm/partial"' in webhook
    assert "get_crm_leads_list" not in check_endpoint
