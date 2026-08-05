"""Tests: assignment_eligibility strictly rejects non-boolean assignment_ready,
INCIERTO, CORREDOR, and manual_review_required docs."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from captacion_assignment_eligibility import assignment_eligibility


def _doc(state, a_ready, **kw):
    return {
        "classification": {
            "state": state,
            "assignment_ready": a_ready,
            "owner_probability": 0.7,
            "source": "structural_rules",
            "decision_source": "structural_rules",
            **kw.get("cls_extra", {}),
        },
        "title": "test title",
        "description": "test description",
        "listing_id": "12345",
        "comuna": "maipu",
        **kw.get("root_extra", {}),
    }


# ===== Non-boolean assignment_ready rejected =====

def test_string_deepseek_not_eligible():
    doc = _doc("DUEÑO_PROBABLE", "deepseek")
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is False
    assert "classification_not_final_or_not_persisted" in reasons


def test_string_true_not_eligible():
    doc = _doc("DUEÑO_PROBABLE", "true")
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is False
    assert "classification_not_final_or_not_persisted" in reasons


def test_int_one_not_eligible():
    doc = _doc("DUEÑO_PROBABLE", 1)
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is False
    assert "classification_not_final_or_not_persisted" in reasons


# ===== INCIERTO eligible with proper confidence (owner potential) =====

def test_incierto_with_true_eligible():
    doc = _doc("INCIERTO", True, cls_extra={"owner_probability": 0.6})
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is True


def test_incierto_wrong_probability_not_eligible():
    doc = _doc("INCIERTO", True, cls_extra={"owner_probability": 0.3})
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is False
    assert "owner_probability_inconsistent_with_state" in reasons


def test_legacy_source_rules_accepted():
    doc = _doc("INCIERTO", True, cls_extra={"owner_probability": 0.6, "source": "rules", "decision_source": None})
    doc["classification"].pop("decision_source", None)
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is True


# ===== CORREDOR never eligible =====

def test_corredor_seguro_not_eligible():
    doc = _doc("CORREDOR_SEGURO", True)
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is False
    assert "classification_not_assignable" in reasons


def test_corredor_probable_not_eligible():
    doc = _doc("CORREDOR_PROBABLE", True)
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is False
    assert "classification_not_assignable" in reasons


# ===== DUEÑO_PROBABLE valid =====

def test_dueno_probable_valid_eligible():
    doc = _doc("DUEÑO_PROBABLE", True)
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is True


def test_dueno_seguro_valid_eligible():
    doc = _doc("DUEÑO_SEGURO", True, cls_extra={"owner_probability": 0.95})
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is True


# ===== manual_review_required blocks =====

def test_manual_review_root_blocks():
    doc = _doc("DUEÑO_PROBABLE", True, root_extra={"manual_review_required": True})
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is False
    assert "manual_review_pending" in reasons


def test_manual_review_cls_blocks():
    doc = _doc("DUEÑO_PROBABLE", True, cls_extra={"manual_review_required": True})
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is False
    assert "manual_review_pending" in reasons


# ===== exclude_from_assignment blocks =====

def test_exclude_cls_blocks():
    doc = _doc("DUEÑO_PROBABLE", True, cls_extra={"exclude_from_assignment": True})
    eligible, reasons = assignment_eligibility(doc)
    assert eligible is False
    assert "explicitly_excluded" in reasons


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
