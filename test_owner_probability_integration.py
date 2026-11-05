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
    # Commercial doc: blocked by CORREDOR gate / commercial identity + low probability
    assert not eligible


def test_magnolia_property_hard_veto_never_becomes_owner_probable():
    item = complete_doc(
        publicador_visible="Magnolia Property",
        seller_name="Magnolia Property",
        seller_type="EMPRESA",
        description="Soy dueño y vendo mi casa sin comisión.",
    )
    item["classification"]["state"] = "DUEÑO_PROBABLE"
    item["classification"]["source"] = "deepseek"
    apply_owner_probability_to_document(item)
    assert item["classification"]["hard_broker_signal"] is True
    assert item["classification"]["hard_veto"] == "PROFESSIONAL"
    assert item["classification"]["state"] == "CORREDOR_SEGURO"
    assert item["classification"]["final_state"] == "CORREDOR_SEGURO"
    assert item["classification"]["assignment_ready"] is False


def test_hard_veto_does_not_match_person_name_alone():
    item = complete_doc(publicador_visible="Juan Pérez", seller_name="Juan Pérez")
    apply_owner_probability_to_document(item)
    assert item["classification"].get("hard_broker_signal") is not True


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


def test_heuristic_score_is_explicitly_labeled_and_never_fills_probability():
    item = {
        "classification": {
            "owner_probability": None,
            "owner_probability_heuristic_score": 85,
            "owner_probability_heuristic_kind": "DETERMINISTIC_SCORE",
            "owner_probability_heuristic_calibrated": False,
            "owner_probability_heuristic_signals": {
                "applied": [{"code": "OWNER_FIRST_PERSON_EXPLICIT", "evidence": "vendo mi casa"}],
            },
        },
    }
    view = build_owner_probability_doc(item)
    assert view["owner_probability_display"] == "85% · heurística"
    assert view["owner_probability_sort"] == 85
    assert "no es una probabilidad calibrada estadísticamente" in view["owner_probability_title"]

    one_point = build_owner_probability_doc({"classification": {"owner_probability_heuristic_score": 1}})
    zero_points = build_owner_probability_doc({"classification": {"owner_probability_heuristic_score": 0}})
    assert one_point["owner_probability_display"] == "1% · heurística"
    assert zero_points["owner_probability_display"] == "0% · heurística"


def test_neutral_baseline_is_displayed_as_percent_and_explained():
    view = build_owner_probability_doc({"classification": {
        "owner_probability": None,
        "owner_probability_heuristic_score": 50,
        "owner_probability_heuristic_kind": "NEUTRAL_BASELINE",
        "owner_probability_heuristic_calibrated": False,
        "owner_probability_heuristic_signals": {"applied": [], "suppressed": [{"code": "SELLER_TYPE_AGENT_OR_COMPANY"}]},
    }})
    assert view["owner_probability_display"] == "50% · heurística (neutral)"
    assert "perfil Profesional de Yapo" in view["owner_probability_title"]


def test_heuristic_score_does_not_unlock_an_assignment_gate():
    item = complete_doc()
    item["classification"].update({
        "state": "DUEÑO_PROBABLE",
        "assignment_ready": True,
        "owner_probability": None,
    })
    before = assignment_eligibility(item)
    item["classification"].update({
        "owner_probability_heuristic_score": 85,
        "owner_probability_heuristic_kind": "DETERMINISTIC_SCORE",
        "owner_probability_heuristic_calibrated": False,
        "owner_probability_heuristic_completeness": {
            "complete": False,
            "reasons": ["DEEPSEEK_REQUIRED_DISABLED"],
        },
    })
    after = assignment_eligibility(item)
    assert before == after
    assert after[0] is False
    assert "owner_probability_missing" in after[1]
