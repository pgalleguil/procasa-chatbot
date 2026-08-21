from pathlib import Path


ROOT = Path(__file__).parents[1]
LIST_TEMPLATE = (ROOT / "templates" / "crm_leads_list.html").read_text(encoding="utf-8")
DETAIL_TEMPLATE = (ROOT / "templates" / "crm_lead_detail.html").read_text(encoding="utf-8")
API_SOURCE = (ROOT / "api_crm.py").read_text(encoding="utf-8")
WEBHOOK_SOURCE = (ROOT / "webhook.py").read_text(encoding="utf-8")


def test_list_has_enviado_with_date_and_time():
    assert '<th class="col-sent">Enviado</th>' in LIST_TEMPLATE
    assert 'lead.effective_sent_date' in LIST_TEMPLATE
    assert 'lead.effective_sent_time' in LIST_TEMPLATE


def test_list_has_operational_column_order_and_independent_response():
    headers = LIST_TEMPLATE[LIST_TEMPLATE.index('<thead>'):LIST_TEMPLATE.index('</thead>')]
    expected = ['Enviado', 'SLA', 'Tipo', 'Cliente', 'Propiedad', 'Estado', 'Última Gestión', 'Ejecutivo', 'Respuesta']
    positions = [headers.index(label) for label in expected]
    assert positions == sorted(positions)
    assert 'class="col-response"' in LIST_TEMPLATE
    response_block = LIST_TEMPLATE[LIST_TEMPLATE.index('data-label="Respuesta"'):]
    assert 'Registrar gestión' in response_block


def test_last_management_is_history_only_and_no_fake_timestamp_without_management():
    assert 'data-label="Última Gestión"' in LIST_TEMPLATE
    history_start = LIST_TEMPLATE.index('<td class="col-last-action" data-label="Última Gestión">')
    history_block = LIST_TEMPLATE[history_start:LIST_TEMPLATE.index('<td class="col-executive"', history_start)]
    assert 'Registrar gestión' not in history_block
    assert '{% if lead.gestionado %}' in history_block
    assert 'Sin gestión' in history_block


def test_list_compact_density_and_filters_remain_available():
    assert 'margin: 2px 0 8px' in LIST_TEMPLATE
    assert 'border-spacing: 0 4px' in LIST_TEMPLATE
    assert 'name="property_code"' in LIST_TEMPLATE
    assert 'data-auto-filter' in LIST_TEMPLATE


def test_sent_timestamp_does_not_use_created_at_as_delivery():
    assert 'effective_sent_at' in API_SOURCE
    assert 'created_at' not in API_SOURCE[API_SOURCE.index('effective_sent_at'):API_SOURCE.index('effective_sent_at') + 1800]
    assert 'effective_sent_source = "Entrega confirmada"' in API_SOURCE
    assert 'effective_sent_source = "Asignación"' in API_SOURCE


def test_quick_management_is_visible_and_uses_canonical_endpoint():
    assert 'data-quick-management' in LIST_TEMPLATE
    assert 'Registrar gestión' in LIST_TEMPLATE
    assert "fetch('/api/crm/management-result'" in LIST_TEMPLATE
    assert 'management_request_id: quickManagementState.managementRequestId' in LIST_TEMPLATE
    assert 'idempotency_key: quickManagementState.managementRequestId' in LIST_TEMPLATE


def test_quick_management_has_progressive_disclosure_and_friendly_results():
    shared_modal = (ROOT / "templates" / "partials" / "crm_quick_management_modal.html").read_text(encoding="utf-8")
    for label in ('No respondió', 'Mensaje enviado / esperando respuesta', 'Contactado',
                  'Requiere seguimiento', 'Visita agendada', 'No interesado', 'Número inválido'):
        assert label in shared_modal
    assert 'quickChannelField' in LIST_TEMPLATE
    assert 'quickDateField' in LIST_TEMPLATE
    assert 'quickReasonField' in LIST_TEMPLATE
    assert 'save.disabled = true' in LIST_TEMPLATE


def test_stale_cycle_is_not_reported_as_success():
    assert "response.status === 409" in LIST_TEMPLATE
    assert 'STALE_ASSIGNMENT_CYCLE' in LIST_TEMPLATE
    assert 'cambió de asignación' in LIST_TEMPLATE
    assert 'status === 409' in DETAIL_TEMPLATE


def test_property_code_is_preserved_in_pagination():
    start = WEBHOOK_SOURCE.index('pagination_query = {', WEBHOOK_SOURCE.index('async def _render_crm_list'))
    block = WEBHOOK_SOURCE[start:start + 500]
    assert '"property_code": property_code' in block


def test_detail_has_one_primary_management_action_and_separate_contact_tools():
    assert 'id="primaryRegisterManagement"' in DETAIL_TEMPLATE
    assert 'Registrar gestión' in DETAIL_TEMPLATE
    assert "openCommPanel('lead', 'phone')" in DETAIL_TEMPLATE
    assert "openCommPanel('lead', 'whatsapp')" in DETAIL_TEMPLATE
    assert "openCommPanel('lead', 'email')" in DETAIL_TEMPLATE


def test_detail_uses_shared_quick_management_and_hides_legacy_main_form():
    assert "partials/crm_quick_management_modal.html" in DETAIL_TEMPLATE
    assert "js/crm_quick_management.js" in DETAIL_TEMPLATE
    assert "onclick=\"openDetailQuickManagement();\"" in DETAIL_TEMPLATE
    assert '<form id="crmForm" class="d-none"' in DETAIL_TEMPLATE
    assert "setActionMode('contact')" not in DETAIL_TEMPLATE.split('id="primaryRegisterManagement"', 1)[1].split('</button>', 1)[0]


def test_shared_component_uses_canonical_contract_and_progressive_fields():
    shared_js = (ROOT / "static" / "js" / "crm_quick_management.js").read_text(encoding="utf-8")
    shared_modal = (ROOT / "templates" / "partials" / "crm_quick_management_modal.html").read_text(encoding="utf-8")
    assert "fetch('/api/crm/management-result'" in shared_js
    assert "management_request_id: submittedId" in shared_js
    assert "idempotency_key: submittedId" in shared_js
    assert "Este lead cambió de asignación. Actualizamos su información." in shared_js
    assert "closeOnStale" in shared_js
    for label in ('No respondió', 'Mensaje enviado / esperando respuesta', 'Contactado',
                  'Requiere seguimiento', 'Visita agendada', 'No interesado', 'Número inválido'):
        assert label in shared_modal
    assert "partials/crm_quick_management_modal.html" in LIST_TEMPLATE
    assert "js/crm_quick_management.js" in LIST_TEMPLATE
    assert "window.CRMQuickManagement.open" in LIST_TEMPLATE
    assert "window.CRMQuickManagement.save" in LIST_TEMPLATE


def test_list_refreshes_last_action_without_reload():
    assert "Última Gestión" in LIST_TEMPLATE
    assert "Recién" in LIST_TEMPLATE
    assert "updateRowAfterQuickManagement" in LIST_TEMPLATE


def test_detail_stale_refreshes_state_without_resubmitting():
    assert "/api/crm/detail-state?phone=" in DETAIL_TEMPLATE
    assert "onStale: refreshDetailManagementState" in DETAIL_TEMPLATE
    assert "GESTIÓN ANTIGUA" not in DETAIL_TEMPLATE
    assert "closeOnStale: true" in DETAIL_TEMPLATE


def test_detail_state_endpoint_exists_and_returns_cycle_fields():
    assert '@app.get("/api/crm/detail-state")' in WEBHOOK_SOURCE
    endpoint = WEBHOOK_SOURCE[WEBHOOK_SOURCE.index('@app.get("/api/crm/detail-state")'):]
    for field in ('assignment_cycle_id', 'crm_estado', 'next_action_date', 'last_action_label'):
        assert f'"{field}"' in endpoint


def test_detail_and_list_share_canonical_result_vocabulary():
    for result in ('CALL_NO_ANSWER', 'MESSAGE_SENT_WAITING_RESPONSE', 'EFFECTIVE_CONTACT',
                   'FOLLOW_UP_REQUESTED', 'VISIT_SCHEDULED', 'NOT_INTERESTED', 'INVALID_NUMBER'):
        assert result in LIST_TEMPLATE
    assert 'management_request_id: managementRequestId' in DETAIL_TEMPLATE
    assert "fetch('/api/crm/update'" in DETAIL_TEMPLATE
