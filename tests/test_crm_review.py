from pathlib import Path

from fastapi.testclient import TestClient

from crm_review import app


ROOT = Path(__file__).parents[1]
REVIEW_SOURCE = (ROOT / "crm_review.py").read_text(encoding="utf-8")
LIST_TEMPLATE = (ROOT / "templates" / "crm_leads_list.html").read_text(encoding="utf-8")
DETAIL_TEMPLATE = (ROOT / "templates" / "crm_lead_detail.html").read_text(encoding="utf-8")


def test_review_list_is_public_and_sanitized():
    with TestClient(app) as client:
        response = client.get("/crm-leads-review?view=list")
    assert response.status_code == 200
    assert "Cliente Demo 01" in response.text
    assert "example.invalid" not in response.text
    assert "CRM_REVIEW_MODE = true" in response.text
    assert "f4121aa" not in response.text


def test_review_detail_is_public_and_uses_real_template():
    with TestClient(app) as client:
        response = client.get("/crm-leads-review?view=detail&lead=review-07")
    assert response.status_code == 200
    assert "Cliente Demo 07" in response.text
    assert "WhatsApp" in response.text
    assert "primaryRegisterManagement" in response.text
    assert "crm_quick_management.js" in response.text


def test_review_health_declares_no_production_dependencies():
    with TestClient(app) as client:
        response = client.get("/api/review-health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "sanitized": True, "mongo": False, "writes": False}


def test_review_app_does_not_import_production_data_or_mutation_services():
    for forbidden in ("MongoClient", "get_db", "get_async_db", "api_crm", "management-result", "send_whatsapp"):
        assert forbidden not in REVIEW_SOURCE
    assert "crm_leads_list.html" in REVIEW_SOURCE
    assert "crm_lead_detail.html" in REVIEW_SOURCE
    assert "crm_quick_management.js" in LIST_TEMPLATE
    assert "crm_quick_management.js" in DETAIL_TEMPLATE


def test_review_has_fake_cases_and_independent_response_column():
    with TestClient(app) as client:
        html = client.get("/crm-leads-review?view=list").text
    for value in ("Vencido", "Próximo · 24 min", "Sin gestión", "No respondió", "Visita agendada", "Cerrado ganado"):
        assert value in html
    header = html[html.index("<thead>"):html.index("</thead>")]
    assert header.index("Asignado") < header.index("Respuesta")
    assert 'data-label="Respuesta"' in html


def test_review_list_has_six_operational_columns_and_merged_cells():
    with TestClient(app) as client:
        html = client.get("/crm-leads-review?view=list").text
    header = html[html.index("<thead>"):html.index("</thead>")]
    expected = ("Asignado", "Prioridad", "Tipo", "Lead", "Gestión", "Respuesta")
    positions = [header.index(fragment) for fragment in (
        '<th class="col-sent">Asignado', '<th class="col-priority">',
        '<th class="col-type">Tipo', '<th class="col-lead">Lead',
        '<th class="col-management">Gestión', '<th class="col-response">Respuesta')]
    assert positions == sorted(positions)
    assert len(__import__("re").findall(r"<th\b", header)) == 6
    assert 'data-label="Cliente"' not in html
    assert 'data-label="Propiedad"' not in html
    assert 'data-label="Estado"' not in html
    assert 'data-label="Última Gestión"' not in html
    assert 'Gestionar' in html


def test_review_cards_and_state_bar_keep_real_query_filters():
    with TestClient(app) as client:
        hot = client.get('/crm-leads-review?view=list&temperatura=HOT')
        managed = client.get('/crm-leads-review?view=list&estado=GRUPO_GESTION')
    assert hot.status_code == managed.status_code == 200
    assert 'Cliente Demo 01' in hot.text and 'Cliente Demo 03' not in hot.text
    assert 'Cliente Demo 05' in managed.text and 'Cliente Demo 01' not in managed.text
    assert 'temperatura=HOT' in hot.text
    assert 'estado=GRUPO_GESTION' in managed.text


def test_review_oldest_assignment_order_uses_assigned_fixture_date():
    with TestClient(app) as client:
        html = client.get('/crm-leads-review?view=list&orden=oldest_assigned').text
    assert html.index('Cliente Demo 09') < html.index('Cliente Demo 01')


def test_review_sla_priority_puts_in_term_before_managed_leads():
    with TestClient(app) as client:
        html = client.get('/crm-leads-review?view=list&orden=sla_priority').text
    assert html.index('Cliente Demo 09') < html.index('Cliente Demo 05')
    assert html.index('Cliente Demo 05') > html.index('Cliente Demo 01')


def test_review_oldest_unmanaged_puts_unmanaged_leads_first_and_oldest():
    with TestClient(app) as client:
        html = client.get('/crm-leads-review?view=list&orden=oldest_unmanaged').text
    assert html.index('Cliente Demo 01') < html.index('Cliente Demo 05')
    assert html.index('Cliente Demo 02') < html.index('Cliente Demo 05')


def test_review_relative_times_use_days_after_24_hours():
    with TestClient(app) as client:
        html = client.get('/crm-leads-review?view=list').text
    assert 'Hace 3 días' in html
    assert 'Hace 3 días 22 h' in html
    assert 'Hace 94 h' not in html


def test_review_management_relative_times_are_dynamic_format():
    with TestClient(app) as client:
        html = client.get('/crm-leads-review?view=list').text
    assert 'Última: No respondió · Hace 25 min' in html
    assert 'Última: Cierre ganado · Hace 1 día 2 h' in html
    assert 'Ayer · 15:20' not in html


def test_priority_info_control_has_compact_accessible_box():
    with TestClient(app) as client:
        html = client.get('/crm-leads-review?view=list').text
    assert 'style="cursor: help; font-size: 0.85rem;"' in html
    assert 'max-height: min(520px, calc(100vh - 90px));' in html
    assert 'border-left: 3px solid var(--accent-color);' in html
    assert '.sla-info-popup::-webkit-scrollbar { width: 6px; }' in html
    assert 'scrollbar-color: var(--accent) rgba(0, 0, 0, 0.05);' in html


def test_filter_bar_exposes_property_code_without_extra_filters_button():
    with TestClient(app) as client:
        html = client.get('/crm-leads-review?view=list').text
    assert 'name="property_code"' in html
    assert '<span class="filter-field-label">Código</span>' in html
    assert 'fa-solid fa-building' not in html
    assert '.filter-bar .property-code-input' in html
    assert 'text-align: center;' in html
    assert 'id="btnMoreFilters"' not in html
    assert 'class="filter-field filter-search-field lead-search-field"' in html
    assert '.filter-bar .lead-search-field { max-width: 220px; }' in html
    assert 'grid-template-columns: 220px 130px repeat(4, 186px) max-content;' in html
    assert '.filter-bar .compact-select-filter { max-width: 186px; }' in html
    assert 'column-gap: 10px;' in html and 'row-gap: 10px;' in html
    assert 'justify-items: stretch;' in html


def test_review_filter_script_keeps_sorting_inside_local_review_route():
    with TestClient(app) as client:
        html = client.get("/crm-leads-review?view=list").text
    assert "const targetPath = window.CRM_REVIEW_MODE ? '/crm-leads-review' : '/crm';" in html


def test_quick_management_includes_property_unavailable_and_keeps_reason_placeholder_empty():
    with TestClient(app) as client:
        html = client.get('/crm-leads-review?view=list').text
    modal = (ROOT / 'templates' / 'partials' / 'crm_quick_management_modal.html').read_text(encoding='utf-8')
    script = (ROOT / 'static' / 'js' / 'crm_quick_management.js').read_text(encoding='utf-8')
    assert 'data-quick-result="PROPERTY_UNAVAILABLE"' in modal
    assert 'data-quick-result="VISIT_SCHEDULED"' in modal
    assert 'id="quickDateLabel"' in modal
    assert "const reasonResults = ['NOT_INTERESTED'];" in script
    assert 'PROPERTY_UNAVAILABLE: new Set' not in script
    assert '<option value="">Seleccionar motivo (opcional)</option>' in modal
    assert 'value="Seleccionar motivo (opcional)"' not in modal
    assert "PROPERTY_UNAVAILABLE: 'Propiedad no disponible'" in script
    assert "if ($('quickReason').value.trim()) details.reason" in script
    assert "PROPERTY_UNAVAILABLE: 'Propiedad no disponible'" in html
    assert 'id="quickDateField"' in modal
    assert 'id="quickNotes"' in modal
    assert 'id="quickFollowUpToggle"' in modal
    assert 'Programar próximo contacto' in modal
    assert "if ($('quickFollowUpToggle')) $('quickFollowUpToggle').checked = false;" in script
    assert "const dateResults = ['EFFECTIVE_CONTACT', 'CALL_NO_ANSWER', 'VISIT_SCHEDULED'];" in script
    assert "details.notes = $('quickNotes').value.trim()" in script


def test_detail_has_one_canonical_cta_and_legacy_form_hidden_by_default():
    with TestClient(app) as client:
        html = client.get("/crm-leads-review?view=detail&lead=review-07").text
    assert html.count('id="primaryRegisterManagement"') == 1
    assert 'id="mode_prop"' in html
    assert 'id="crmForm" class="d-none"' in html
    assert '#crmForm { display: none !important; }' in html
    assert '#crmForm.owner-mode-visible' in html
    assert "legacy-management-heading" in html
    for label in ("Autoriza", "Restricciones", "No Responde", "No Autoriza", "Legal / Tec", "No Disponible"):
        assert label in html
