"""P5 regression tests for canonical state precedence and assignment safety."""
from __future__ import annotations

import unittest

from crm_schema import build_crm_document, normalize_classification
from mongo_store import validate_property_for_canonical_insert
from owner_probability import apply_owner_probability_to_document


class P5PersistenceInvariantTests(unittest.TestCase):
    def test_4298432_structural_professional_survives_owner_probability(self):
        raw = {
            "listing_id": "4298432",
            "url": "https://www.toctoc.com/propiedades/arriendoparticularsr/departamento/penalolen/departamento-jose-arrieta-9870/4298432",
            "title": "Departamento, Jose arrieta 9870",
            "description": "Departamento ubicado en Peñalolén. Arrienda corredora. Contacto Anunciante - Particular Daniela Cordero.",
            "operacion": "arriendo",
            "tipo_propiedad": "departamento",
            "comuna": "Peñalolén",
            "region": "Metropolitana",
            "seller_type": "PARTICULAR",
            "classification": {
                "state": "CORREDOR_SEGURO",
                "confidence": 0.95,
                "reason": "Evidencia profesional explícita: arrienda corredora",
                "evidence": ["arrienda corredora"],
                "source": "structural_rules",
                "decision_pattern": "strong_professional_signal",
                "strong_signal_found": True,
                "rule_state": "CORREDOR_SEGURO",
            },
        }
        doc = build_crm_document(raw)
        apply_owner_probability_to_document(doc, extracted=raw)
        cls = doc["classification"]

        self.assertEqual(cls["state"], "CORREDOR_SEGURO")
        self.assertEqual(cls["final_state"], "CORREDOR_SEGURO")
        self.assertEqual(cls["hard_veto"], "PROFESSIONAL")
        self.assertFalse(cls["assignment_ready"])
        self.assertEqual(cls["owner_probability"], 0.55)
        self.assertEqual(validate_property_for_canonical_insert(doc), [])

    def test_assignment_ready_contradictions_are_rejected_defensively(self):
        for state, error_code in (
            ("INCIERTO", "ASSIGNMENT_READY_INVALID_FOR_INCIERTO"),
            ("CORREDOR_SEGURO", "ASSIGNMENT_READY_INVALID_FOR_CORREDOR_SEGURO"),
            ("CORREDOR_PROBABLE", "ASSIGNMENT_READY_INVALID_FOR_CORREDOR_PROBABLE"),
            ("AD_REMOVED", "ASSIGNMENT_READY_INVALID_FOR_AD_REMOVED"),
        ):
            doc = {
                "listing_id": "9999999",
                "url": "https://example.test/9999999",
                "comuna": "Peñalolén",
                "operacion": "arriendo",
                "tipo_propiedad": "departamento",
                "title": "Aviso",
                "classification": {
                    "state": state,
                    "confidence": 0.55 if state == "INCIERTO" else 0.10,
                    "source": "rules",
                    "assignment_ready": True,
                },
            }
            errors = validate_property_for_canonical_insert(doc)
            self.assertIn(error_code, errors, state)

    def test_owner_states_can_be_ready_only_after_owner_gate(self):
        for state, probability in (("DUEÑO_PROBABLE", 0.70), ("DUEÑO_SEGURO", 0.90)):
            cls = normalize_classification({
                "state": state,
                "owner_probability": probability,
                "source": "deepseek",
                "deepseek_status": "VALID",
                "deepseek_raw": {"choices": [{}]},
            })
            self.assertTrue(cls["assignment_ready"], state)


if __name__ == "__main__":
    unittest.main()
