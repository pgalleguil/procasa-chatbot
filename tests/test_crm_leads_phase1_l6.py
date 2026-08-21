import re

from fastapi.testclient import TestClient

from crm_review import app


def page(query="view=list"):
    with TestClient(app) as client:
        return client.get(f"/crm-leads-review?{query}").text


def card_blocks(html):
    return re.findall(r'<a href="[^"]+" class="summary-card[^>]*>(.*?)</a>', html, re.S)


def test_cards_follow_executive_composition_and_shared_size_contract():
    html = page()
    assert html.count('class="summary-card') == 3
    assert "min-height: 146px" in html and "height: 146px" in html
    assert "gap: 18px" in html
    assert "padding: 17px 21px 15px" in html
    assert "Últimos 7 días" in html
    assert "Asignaciones por día · últimos 7 días" not in html
    assert "fa-users fa-2x" not in html and "fa-user fa-2x" not in html


def test_secondary_copy_is_below_kpi_and_sparkline_has_seven_points():
    html = page()
    for block in card_blocks(html):
        value = block.index("summary-value")
        meta = block.index("summary-meta")
        sparkline = block.index("summary-sparkline")
        assert value < meta < sparkline
        assert block.count("<polyline ") == 1
        assert len(re.search(r'points="([^"]+)"', block).group(1).split()) == 7


def test_active_state_and_keyboard_contract_are_preserved():
    html = page()
    assert html.count('class="summary-card is-active"') == 1
    assert "border: 2px solid var(--accent-color)" in html
    assert "outline: none" in html
    assert html.count("onkeydown=\"if(event.key===' ')") == 3
    assert html.count('aria-pressed=') == 3
    assert 'class="summary-card is-active"' in page("view=list&temperatura=HOT")
    assert 'class="summary-card is-active"' in page("view=list&temperatura=COLD")


def test_mobile_contract_hides_sparklines_and_keeps_three_columns():
    html = page()
    assert "grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px" in html
    assert "height: 70px" in html
    assert ".temperature-cards .summary-sparkline" in html
    assert ".summary-footer { display: none; }" in html
