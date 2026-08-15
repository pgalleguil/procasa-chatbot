"""P2 classification semantics tests; no Mongo writes or network calls."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scraper_toctoc"))

from crm_schema import normalize_classification  # noqa: E402
from mongo_store import validate_property_for_canonical_insert  # noqa: E402
from owner_probability import apply_owner_probability_to_document, expected_state_for_probability  # noqa: E402


def base_doc(classification: dict) -> dict:
    return {
        "listing_id": "123456", "url": "https://example.test/123456",
        "comuna": "macul", "operacion": "venta", "tipo_propiedad": "departamento",
        "title": "Departamento en Macul", "description": "Descripción suficiente de la propiedad.",
        "classification": classification,
    }


class P2ClassificationTests(unittest.TestCase):
    def test_dueno_probable_normalizes_without_degradation(self):
        normalized = normalize_classification({
            "state": "DUEÑO_PROBABLE", "confidence": 0.82,
            "source": "rules_json", "evidence": ["test"],
        })
        self.assertEqual(normalized["state"], "DUEÑO_PROBABLE")

    def test_owner_probability_82_sets_canonical_state_and_confidence(self):
        normalized = normalize_classification({
            "state": "INCIERTO", "confidence": 0.50,
            "rule_confidence": 0.50, "owner_probability": 82,
            "source": "deterministic_evidence_engine",
        })
        self.assertEqual(normalized["state"], "DUEÑO_PROBABLE")
        self.assertEqual(normalized["confidence"], 0.82)
        self.assertEqual(normalized["rule_confidence"], 0.50)

    def test_probability_95_and_60(self):
        self.assertEqual(expected_state_for_probability(0.95), "DUEÑO_SEGURO")
        self.assertEqual(expected_state_for_probability(0.60), "INCIERTO")

    def test_all_band_boundaries(self):
        expected = {
            0.19: "CORREDOR_SEGURO", 0.20: "CORREDOR_PROBABLE",
            0.49: "CORREDOR_PROBABLE", 0.50: "INCIERTO",
            0.69: "INCIERTO", 0.70: "DUEÑO_PROBABLE",
            0.89: "DUEÑO_PROBABLE", 0.90: "DUEÑO_SEGURO",
        }
        for probability, state in expected.items():
            self.assertEqual(expected_state_for_probability(probability), state)

    def test_previous_technical_confidence_does_not_fail_canonical_validation(self):
        result = {
            "owner_probability": 0.82,
            "owner_probability_completeness": {"reasons": []},
            "owner_probability_band": "70-89",
            "owner_probability_expected_state": "DUEÑO_PROBABLE",
            "owner_probability_signals": {"applied": []},
        }
        doc = base_doc({"state": "INCIERTO", "confidence": 0.50, "source": "rules_json"})
        with patch("owner_probability.calculate_owner_probability", return_value=result):
            apply_owner_probability_to_document(doc)
        self.assertEqual(doc["classification"]["state"], "DUEÑO_PROBABLE")
        self.assertEqual(doc["classification"]["confidence"], 0.82)
        self.assertEqual(doc["classification"]["rule_confidence"], 0.50)
        self.assertNotIn("DUEÑO_PROBABLE_CONFIDENCE_TOO_LOW", validate_property_for_canonical_insert(doc))

    def test_pending_llm_is_not_canonical_classification(self):
        errors = validate_property_for_canonical_insert(base_doc({
            "status": "PENDING_LLM", "source": "dry_run_rules",
        }))
        self.assertIn("MISSING_CLASSIFICATION_STATE", errors)
        self.assertIn("NON_FINAL_CLASSIFICATION_STATUS(PENDING_LLM)", errors)

    def test_processing_skips_do_not_become_classifications(self):
        for status in ("HISTORICAL_DUPLICATE", "SKIP_PROFESSIONAL", "DOWNLOAD_FAILED"):
            errors = validate_property_for_canonical_insert(base_doc({}))
            self.assertIn("MISSING_CLASSIFICATION_STATE", errors, status)

    def test_normalized_states_contain_no_mojibake(self):
        normalized = normalize_classification({"state": "DUEÑO_SEGURO", "confidence": 0.95})
        self.assertNotIn("DUEÃ", normalized["state"])
        self.assertEqual(normalized["state"], "DUEÑO_SEGURO")


if __name__ == "__main__":
    unittest.main()
