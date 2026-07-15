from captacion_assignment_eligibility import assignment_eligibility
from owner_confidence import build_owner_probability_doc
from owner_probability import apply_owner_probability_to_document
from pathlib import Path


def complete_doc(**overrides):
    item = {
        "listing_id": "integration-1",
        "url": "https://example.test/integration-1",
        "origen": "yapo",
        "comuna": "Talca",
        "description": "Arriendo mi departamento directamente como propietario.",
        "seller_name": "Juan Pérez",
        "seller_type": "PARTICULAR",
        "scrape_stage": "classification_done",
        "html_validation_status": "OK",
        "classification": {
            "state": "INCIERTO",
            "rule_state": "INCONCLUSIVE",
            "source": "deepseek",
            "deepseek_status": "VALID",
            "deepseek_structured_evidence": [],
        },
    }
    item.update(overrides)
    return item


def test_final_gate_derives_state_and_preserves_technical_confidence():
    item = complete_doc()
    item["classification"]["confidence"] = 0.73
    apply_owner_probability_to_document(item)
    assert item["classification"]["owner_probability"] >= 0.90
    assert item["classification"]["state"] == "DUEÑO_SEGURO"
    assert item["classification"]["confidence"] == 0.73
    assert item["classification"]["assignment_ready"] is True


def test_incomplete_record_is_pending_not_neutral_50():
    item = complete_doc(description="", seller_name="")
    apply_owner_probability_to_document(item)
    assert item["classification"]["owner_probability"] is None
    assert item["classification"]["state"] == "PENDIENTE"
    assert item["classification"]["assignment_ready"] is False
    assert build_owner_probability_doc(item)["owner_probability_display"] == "S/I"


def test_assignment_gate_requires_probability_at_least_50():
    item = complete_doc(description="Corredora cobra comisión por corretaje.", company_name="ACME Propiedades")
    apply_owner_probability_to_document(item)
    eligible, reasons = assignment_eligibility(item)
    assert eligible is False
    assert item["classification"]["owner_probability"] < 0.50
    assert "owner_probability_below_50" in reasons


def test_view_uses_probability_and_signal_tooltip():
    item = complete_doc()
    apply_owner_probability_to_document(item)
    view = build_owner_probability_doc(item)
    assert view["owner_probability_display"].endswith("%")
    assert view["owner_probability_sort"] >= 90
    assert "OWNER_FIRST_PERSON_EXPLICIT" in view["owner_probability_title"]


def test_list_and_detail_share_the_same_owner_probability_display():
    item = complete_doc()
    item["classification"]["owner_probability"] = 0.95
    view = build_owner_probability_doc(item)
    list_display = view["owner_probability_display"]
    detail_display = view["owner_probability_display"]
    assert list_display == detail_display == "95%"


def test_half_and_missing_probability_format_without_fallback():
    assert build_owner_probability_doc(
        {"classification": {"owner_probability": 0.50}}
    )["owner_probability_display"] == "50%"
    assert build_owner_probability_doc(
        {"classification": {"owner_probability": None, "confidence": 0.95, "owner_score": 95}}
    )["owner_probability_display"] == "S/I"


def test_detail_indicator_uses_only_owner_probability_view_fields():
    template = Path("templates/captacion_detail.html").read_text(encoding="utf-8")
    marker = "<!-- Owner probability overlay: same source and formatting as the list -->"
    block = template.split(marker, 1)[1].split("</div>", 1)[0]
    assert "prop.owner_probability_display" in block
    assert "prop.owner_probability_sort" in block
    assert "prop.score_captacion" not in block
    assert "classification.confidence" not in block
    assert ">50<" not in block
