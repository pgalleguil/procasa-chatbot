import re
from fastapi.testclient import TestClient
from crm_review import app


def page(query="view=list"):
    with TestClient(app) as client:
        return client.get(f"/crm-leads-review?{query}").text


def test_cards_have_single_active_state_and_seven_sparkline_buckets():
    html = page()
    assert html.count('class="summary-card is-active"') == 1
    assert html.count('class="summary-sparkline"') == 1
    assert html.count('class="summary-sparkline hot"') == 1
    assert html.count('class="summary-sparkline cold"') == 1
    assert len(re.findall(r'<div class="summary-sparkline[^>]*>.*?</div>', html, re.S)) == 3
    for block in re.findall(r'<div class="summary-sparkline[^>]*>(.*?)</div>', html, re.S):
        assert block.count('<polyline ') == 1
        assert len(re.search(r'points="([^"]+)"', block).group(1).split()) == 7


def test_assignment_date_has_full_year_and_relative_metadata():
    html = page()
    assert '20/08/2026' in html
    assert 'class="sent-relative"' in html
    assert 'class="sent-source"' not in html
    assert 'effective_sent_date' in open('api_crm.py', encoding='utf-8').read()


def test_priority_has_operational_timing_copy():
    html = page()
    assert 'class="sla-timing"' in html
    assert any(value in html for value in ('Venció hace', 'Faltan', 'Dentro de SLA'))


def test_temperature_filter_card_select_and_rows_stay_synchronized():
    hot = page('view=list&temperatura=HOT')
    cold = page('view=list&temperatura=COLD')
    assert 'value="HOT" selected' in hot
    assert 'value="COLD" selected' in cold
    assert 'Cliente Demo 01' in hot and 'Cliente Demo 03' not in hot
    assert 'Cliente Demo 03' in cold and 'Cliente Demo 01' not in cold
    assert '🔥 Lead Hot' not in hot and '🔥 Lead Hot' not in cold


def test_quick_response_has_exact_contact_outcomes_and_no_channel():
    html = page()
    assert re.findall(r'data-quick-result="([A-Z_]+)"', html) == [
        'EFFECTIVE_CONTACT', 'CALL_NO_ANSWER', 'NOT_INTERESTED', 'INVALID_NUMBER', 'OTHER_EXPLICIT']
    assert 'data-quick-result="MESSAGE_SENT_WAITING_RESPONSE"' not in html
    assert 'id="quickChannelField"' not in html
    assert 'quickOtherField' in html
    assert 'maxlength="180"' in html


def test_quick_reason_options_and_detail_link_are_present():
    html = page()
    for value in ('Ya no busca', 'Esta propiedad no le interesa', 'Precio o condiciones', 'Ya encontró otra propiedad',
                  'Número inexistente', 'Número equivocado', 'No corresponde al cliente'):
        assert value in html
    assert 'quickGoDetail' in html
    assert 'return_url' in open('static/js/crm_quick_management.js', encoding='utf-8').read()
