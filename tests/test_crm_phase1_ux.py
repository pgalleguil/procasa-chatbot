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
    for label in ('No respondió', 'Mensaje enviado / esperando respuesta', 'Contactado',
                  'Requiere seguimiento', 'Visita agendada', 'No interesado', 'Número inválido'):
        assert label in LIST_TEMPLATE
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


def test_detail_and_list_share_canonical_result_vocabulary():
    for result in ('CALL_NO_ANSWER', 'MESSAGE_SENT_WAITING_RESPONSE', 'EFFECTIVE_CONTACT',
                   'FOLLOW_UP_REQUESTED', 'VISIT_SCHEDULED', 'NOT_INTERESTED', 'INVALID_NUMBER'):
        assert result in LIST_TEMPLATE
    assert 'management_request_id: managementRequestId' in DETAIL_TEMPLATE
    assert "fetch('/api/crm/update'" in DETAIL_TEMPLATE
