from datetime import datetime, timezone

import pytest

from owner_evidence_deepseek import _quote_in_source
from owner_probability import calculate_owner_probability


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def doc(description="Descripción completa de una propiedad sin otras señales.", **values):
    result = {
        "origen": "yapo",
        "listing_id": "test",
        "description": description,
        "seller_name": "Ana Pérez",
        "html_validation_status": "OK",
        "scrape_stage": "classification_done",
        "classification": {
            "state": "INCIERTO",
            "rule_state": "INCONCLUSIVE",
            "decision_source": "deepseek",
            "deepseek_status": "VALID",
            "deepseek_raw": {"choices": [{"message": {"content": "{}"}}]},
        },
    }
    result.update(values)
    return result


def test_neutral_complete_is_exactly_fifty():
    item = doc(seller_name="Particular")
    result = calculate_owner_probability(item, calculated_at=NOW)
    assert result["owner_probability"] == 0.5
    assert result["owner_probability_evidence_quality"] == "COMPLETE_NEUTRAL"


def test_incomplete_is_none_never_fifty():
    item = doc(description="", seller_name="")
    result = calculate_owner_probability(item, calculated_at=NOW)
    assert result["owner_probability"] is None
    assert result["owner_probability_band"] == "S/I"


def test_explicit_owner_ranks_high():
    item = doc("Soy dueño y vendo mi casa sin comisión.", seller_type="PARTICULAR")
    result = calculate_owner_probability(item, calculated_at=NOW)
    assert result["owner_probability"] >= 0.9
    assert result["owner_probability_band"] == "90-100"


def test_commercial_identity_is_not_double_counted():
    item = doc(
        company_name="Casa Propiedades SpA",
        broker_brand="Casa Propiedades",
        seller_name="Casa Propiedades",
    )
    result = calculate_owner_probability(item, calculated_at=NOW)
    signals = result["owner_probability_signals"]["applied"]
    commercial = [s for s in signals if s["family"] == "commercial_identity"]
    assert len(commercial) == 1
    assert commercial[0]["weight"] == -60


def test_activity_never_penalizes_on_its_own():
    item = doc(
        publisher_activity={"window_days": 90, "distinct_properties_in_window": 12},
        seller_name="Particular",
    )
    result = calculate_owner_probability(item, calculated_at=NOW)
    assert result["owner_probability"] == 0.5
    assert not any(
        signal["code"] == "EIGHT_OR_MORE_PROPERTIES_90D"
        for signal in result["owner_probability_signals"]["applied"]
    )


def test_incierto_below_fifty_is_reported_not_adjusted():
    item = doc("Se cobra comisión de corretaje.", seller_name="Particular")
    result = calculate_owner_probability(item, calculated_at=NOW)
    assert result["owner_probability"] == 0.1
    assert result["owner_probability_contradiction"] == "INCIERTO_BELOW_50"


@pytest.mark.parametrize(
    "status",
    ["PENDING", "API_ERROR", "INVALID_JSON"],
)
def test_required_deepseek_failure_is_si(status):
    item = doc()
    item["classification"]["deepseek_status"] = status
    item["classification"].pop("deepseek_raw")
    result = calculate_owner_probability(item, calculated_at=NOW)
    assert result["owner_probability"] is None
    assert any(status in reason for reason in result["owner_probability_completeness"]["reasons"])


def test_confidence_never_changes_probability():
    low = doc()
    high = doc()
    low["classification"]["confidence"] = 0.1
    high["classification"]["confidence"] = 0.99
    assert calculate_owner_probability(low, calculated_at=NOW)["owner_probability"] == calculate_owner_probability(high, calculated_at=NOW)["owner_probability"]


def test_historic_valid_deepseek_structured_fields_are_accepted():
    item = doc()
    item["classification"].pop("deepseek_status")
    item["classification"].pop("deepseek_raw")
    item["classification"]["semantic_check"] = {
        "status": "VALID", "required": True, "reason": "Resultado estructurado válido",
    }
    item["classification"]["reason"] = "Resultado estructurado válido"
    item["classification"]["evidence"] = ["seller_type=PARTICULAR"]
    result = calculate_owner_probability(item, calculated_at=NOW)
    assert result["owner_probability"] is not None
    assert result["owner_probability_completeness"]["deepseek_status"] == "VALID"


def test_no_commission_of_brokerage_is_positive_not_fee():
    item = doc("Sin comisión de corretaje, venta directa.", seller_type="PARTICULAR")
    result = calculate_owner_probability(item, calculated_at=NOW)
    codes = {signal["code"] for signal in result["owner_probability_signals"]["applied"]}
    assert "OWNER_NO_COMMISSION_EXPLICIT" in codes
    assert "COMMISSION_OR_BROKERAGE_FEES" not in codes


def test_professional_badge_blocks_personal_identity_bonus():
    item = doc(seller_name="Ana Pérez", seller_type="PROFESIONAL", seller_is_pro=True)
    result = calculate_owner_probability(item, calculated_at=NOW)
    codes = {signal["code"] for signal in result["owner_probability_signals"]["applied"]}
    assert "PROFESSIONAL_BADGE" in codes
    assert "PERSONAL_IDENTITY_NO_COMMERCIAL" not in codes


def test_listing_jsonld_title_is_not_commercial_identity():
    item = doc(seller_name="Nicole Reyes", seller_jsonld_name="Remodelado sin corredor en Plaza Egaña")
    result = calculate_owner_probability(item, calculated_at=NOW)
    codes = {signal["code"] for signal in result["owner_probability_signals"]["applied"]}
    assert "EXPLICIT_COMMERCIAL_IDENTITY" not in codes


def test_vendo_como_propietario_is_explicit_owner():
    item = doc("Vendo como propietario, sin comisión.", seller_type="PARTICULAR")
    result = calculate_owner_probability(item, calculated_at=NOW)
    assert result["owner_probability"] == 1.0


def test_sin_corredora_de_propiedades_is_not_commercial_description():
    item = doc("Venta sin corredora de propiedades, trato directo con el dueño.", seller_type="PARTICULAR")
    result = calculate_owner_probability(item, calculated_at=NOW)
    codes = {signal["code"] for signal in result["owner_probability_signals"]["applied"]}
    assert "COMMERCIAL_DESCRIPTION" not in codes
    assert "OWNER_NO_COMMISSION_EXPLICIT" in codes


def test_single_profile_bonus_requires_complete_activity_coverage():
    item = doc(
        seller_name="Particular",
        publisher_activity={
            "window_days": 90, "distinct_properties_in_window": 1, "coverage_complete": False,
        },
    )
    result = calculate_owner_probability(item, calculated_at=NOW)
    assert result["owner_probability"] == 0.5


def test_owner_signoff_is_explicit_self_identification():
    item = doc("Consultas al teléfono. Atte., uno de los propietarios.", seller_type="PARTICULAR")
    result = calculate_owner_probability(item, calculated_at=NOW)
    codes = {signal["code"] for signal in result["owner_probability_signals"]["applied"]}
    assert "OWNER_FIRST_PERSON_EXPLICIT" in codes


def test_structured_deepseek_evidence_is_weighted_but_not_a_probability():
    item = doc("Texto ambiguo del aviso.", seller_name="Particular")
    item["classification"]["deepseek_status"] = "ERROR"
    result = calculate_owner_probability(
        item,
        extracted={
            "deepseek_structured_evidence_status": "VALID",
            "deepseek_structured_evidence": [{
                "code": "COMMERCIAL_DESCRIPTION",
                "source_field": "description",
                "quote": "asesoría inmobiliaria",
            }],
        },
        calculated_at=NOW,
    )
    assert result["owner_probability"] == 0.25
    assert result["owner_probability_completeness"]["deepseek_status"] == "VALID"


def test_deepseek_quote_validation_tolerates_one_encoding_replacement_only():
    assert _quote_in_source("sin comisión", "Venta directa, sin comisi?n de corretaje")
    assert not _quote_in_source("somos una inmobiliaria", "Venta directa, sin comisión")
