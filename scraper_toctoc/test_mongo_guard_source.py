"""Tests for validate_property_for_canonical_insert: source/decision_source compat."""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "scraper_toctoc"))
os.environ.setdefault("DEEPSEEK_ENABLED", "false")
os.environ.setdefault("MONGO_URI", "")

from mongo_store import validate_property_for_canonical_insert, validate_classification_probability_consistency


def _base(**overrides):
    base = {
        "listing_id": "12345",
        "url": "https://www.toctoc.com/propiedades/test/12345",
        "comuna": "maipu",
        "operacion": "venta",
        "tipo_propiedad": "departamento",
        "title": "Test property",
        "description": "Test description for property.",
        "classification": {
            "state": "INCIERTO",
            "confidence": 0.55,
        },
    }
    # Deep-merge classification if provided
    cls = {**base["classification"], **overrides.pop("classification", {})}
    base["classification"] = cls
    base.update(overrides)
    return base


# 1. source valid -> accepted
def test_source_valid_passes():
    doc = _base(classification={"source": "structural_rules"})
    assert validate_property_for_canonical_insert(doc) == []


# 2. decision_source valid -> accepted (compat fix)
def test_decision_source_valid_passes():
    doc = _base(classification={"decision_source": "deepseek", "source": None})
    doc["classification"].pop("source", None)
    assert validate_property_for_canonical_insert(doc) == []


# 3. both absent -> rejected
def test_both_absent_rejected():
    doc = _base(classification={"source": None, "decision_source": None})
    doc["classification"].pop("source", None)
    doc["classification"].pop("decision_source", None)
    errors = validate_property_for_canonical_insert(doc)
    assert "MISSING_CLASSIFICATION_SOURCE" in errors


# 4. source not allowed (url_path_signal) -> rejected
def test_url_path_signal_rejected():
    doc = _base(classification={"source": "url_path_signal"})
    errors = validate_property_for_canonical_insert(doc)
    assert "CLASSIFICATION_FROM_URL_PATH_ONLY" in errors


# 5. INCIERTO with too-low confidence -> rejected
def test_incierto_confidence_too_low_rejected():
    doc = _base(classification={"source": "rules", "confidence": 0.2})
    errors = validate_property_for_canonical_insert(doc)
    assert any("CONFIDENCE_TOO_LOW" in e for e in errors)


# 6. INCIERTO with too-high confidence -> rejected
def test_incierto_confidence_too_high_rejected():
    doc = _base(classification={"source": "rules", "confidence": 0.95})
    errors = validate_property_for_canonical_insert(doc)
    assert any("CONFIDENCE_TOO_HIGH" in e for e in errors)


# 7. DUEÑO_PROBABLE with valid confidence -> passes
def test_dueno_probable_valid_passes():
    doc = _base(classification={"source": "rules", "state": "DUEÑO_PROBABLE", "confidence": 0.75})
    assert validate_property_for_canonical_insert(doc) == []


# 8. DUEÑO_SEGURO with low confidence -> rejected
def test_dueno_seguro_low_conf_rejected():
    doc = _base(classification={"source": "rules", "state": "DUEÑO_SEGURO", "confidence": 0.5})
    errors = validate_property_for_canonical_insert(doc)
    assert any("CONFIDENCE" in e for e in errors)


# 9. CORREDOR with empty source via decision_source -> passes (compat)
def test_corredor_with_decision_source_passes():
    doc = _base(classification={"decision_source": "structural_rules", "state": "CORREDOR_SEGURO", "confidence": 0.95})
    doc["classification"].pop("source", None)
    errors = validate_property_for_canonical_insert(doc)
    assert any("CORREDOR_SEGURO_CONFIDENCE_OUT_OF_RANGE" in e for e in errors)


# 10. Missing title+description -> rejected
def test_missing_title_and_description_rejected():
    doc = _base(title="", description="", classification={"source": "rules"})
    errors = validate_property_for_canonical_insert(doc)
    assert "MISSING_TITLE_AND_DESCRIPTION" in errors


# 11. Historical normal doc -> unchanged behavior
def test_normal_doc_passes():
    doc = _base(classification={"source": "structural_rules", "rule_state": "INCONCLUSIVE"})
    assert validate_property_for_canonical_insert(doc) == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
