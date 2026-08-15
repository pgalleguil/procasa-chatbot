"""Regression tests for owner identity versus generic selling language."""
import sys
import unittest

sys.path.insert(0, r"C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok")
from owner_evidence_deepseek import (
    classify_owner_phrase,
    extract_explicit_professional_evidence,
    _validate_evidence,
)


class TestOwnerEvidenceSemantics(unittest.TestCase):
    def test_generic_selling_is_not_explicit_owner(self):
        for phrase in (
            "Vendo departamento en Macul",
            "Vendo conversable",
            "Se vende departamento",
            "Venta departamento excelente ubicación",
            "Precio conversable",
        ):
            self.assertEqual(classify_owner_phrase(phrase), "GENERIC_SELLING_LANGUAGE")

    def test_explicit_owner_identity(self):
        for phrase in (
            "Soy dueño y vendo directamente",
            "Soy propietario del departamento",
            "Dueño directo vende",
            "Vende su dueño",
            "Venta directa por propietario",
        ):
            self.assertEqual(classify_owner_phrase(phrase), "EXPLICIT_OWNER_IDENTITY")

    def test_supporting_only(self):
        for phrase in ("Sin comisión", "Sin intermediarios", "Trato directo", "No corredores"):
            self.assertEqual(classify_owner_phrase(phrase), "OWNER_SUPPORTING")

    def test_possessive_is_supporting_not_explicit(self):
        self.assertEqual(classify_owner_phrase("Vendo mi departamento"), "OWNER_POSSESSIVE_SUPPORTING")

    def test_validation_rejects_generic_owner_code(self):
        valid, rejected = _validate_evidence(
            {"evidence": [{"code": "OWNER_FIRST_PERSON_EXPLICIT", "source_field": "description", "quote": "Vendo departamento", "explanation": ""}]},
            {"description": "Vendo departamento en Macul"},
        )
        self.assertEqual(valid, [])
        self.assertIn("generic_selling_not_owner:OWNER_FIRST_PERSON_EXPLICIT", rejected)

    def test_true_owner_phrase_survives(self):
        valid, rejected = _validate_evidence(
            {"evidence": [{"code": "OWNER_FIRST_PERSON_EXPLICIT", "source_field": "description", "quote": "VENDE DUEÑO DIRECTO", "explanation": ""}]},
            {"description": "VENDE DUEÑO DIRECTO SIN COMISIONES"},
        )
        self.assertEqual(rejected, [])
        self.assertEqual(valid[0]["evidence_type"], "EXPLICIT_OWNER_IDENTITY")

    def test_quote_not_found_is_rejected(self):
        valid, rejected = _validate_evidence(
            {"evidence": [{"code": "OWNER_FIRST_PERSON_EXPLICIT", "source_field": "description", "quote": "Soy propietario", "explanation": ""}]},
            {"description": "Departamento en venta"},
        )
        self.assertEqual(valid, [])
        self.assertTrue(any(item.startswith("QUOTE_NOT_FOUND_IN_SOURCE:") for item in rejected))

    def test_taxonomy_overrides_wrong_model_code(self):
        valid, rejected = _validate_evidence(
            {"evidence": [{"code": "OWNER_NO_COMMISSION_EXPLICIT", "source_field": "description", "quote": "DUEÑO DIRECTO", "explanation": ""}]},
            {"description": "DUEÑO DIRECTO"},
        )
        self.assertEqual(rejected, [])
        self.assertEqual(valid[0]["code"], "OWNER_FIRST_PERSON_EXPLICIT")

    def test_explicit_professional_signals(self):
        for phrase in (
            "Corredor 9 7609 3628",
            "Contactar con su corredora Patricia Canal",
            "Se cobra comisión de corretaje",
            "Honorarios de corretaje 2%",
            "Agente inmobiliaria María Pérez",
            "Corredora de propiedades",
        ):
            evidence = extract_explicit_professional_evidence({"description": phrase})
            self.assertTrue(evidence, phrase)

    def test_company_person_phone_professional_pattern(self):
        evidence = extract_explicit_professional_evidence({
            "description": "Habitacura Propiedades. Alvaro Yáñez. 569 75240399"
        })
        self.assertTrue(evidence)
        self.assertEqual(evidence[0]["code"], "EXPLICIT_COMMERCIAL_IDENTITY")

    def test_developer_inmobiliaria_alone_is_not_professional(self):
        evidence = extract_explicit_professional_evidence({
            "description": "Proyecto desarrollado por Inmobiliaria Barlovento S.A."
        })
        self.assertEqual(evidence, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
