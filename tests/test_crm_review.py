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
    assert header.index("Enviado") < header.index("Respuesta")
    assert 'data-label="Respuesta"' in html
