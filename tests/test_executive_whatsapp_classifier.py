"""Contratos del clasificador: pruebas totalmente offline."""
from unittest import TestCase

from chatbot.executive_whatsapp_classifier import (
    prepare_audit_input, validate_audit_result,
)


def lead_example():
    return {
        "_id": "L1",
        "messages": [
            {"role": "assistant", "content": "¿En qué puedo ayudarte?",
             "timestamp": "2026-11-01T10:00:00Z"}
        ],
        "executive_whatsapp_messages": [
            {
                "actor_type": "executive", "content": "Te contacto por tu consulta",
                "timestamp": "2026-11-03T09:55:00Z",
                "provider_key": "session:messages.upsert:phone:msg0",
                "executive_user_id": "U1", "assignment_cycle_id": "C1",
            },
            {
                "actor_type": "client", "content": "Todavía busco en San Miguel",
                "timestamp": "2026-11-03T10:00:00Z",
                "provider_key": "session:messages.upsert:phone:msg1",
                "executive_user_id": "U1", "assignment_cycle_id": "C1",
            }
        ],
    }


def example_result(prepared, **kwargs):
    result = {
        "commercial_status": "ACTIVE_INTEREST",
        "contact_effective": True,
        "follow_up_required": True,
        "opt_out": False,
        "next_action": "Revisar seguimiento",
        "preferences": {"comuna": "San Miguel"},
        "evidence_provider_keys": [
            "session:messages.upsert:phone:msg0",
            "session:messages.upsert:phone:msg1",
        ],
        "uncertainty": "No hay confirmación de cobertura histórica completa",
        "coverage_complete": prepared["coverage_complete"],
    }
    result.update(kwargs)
    return result


class TestExecutiveAuditClassifier(TestCase):
    def test_prepares_separate_sources_and_unverified_coverage(self):
        prepared = prepare_audit_input(lead_example())
        self.assertFalse(prepared["coverage_complete"])
        self.assertEqual(
            [x["actor_type"] for x in prepared["messages"]],
            ["chatbot", "executive", "client"],
        )

    def test_validates_grounded_classification_for_manual_review(self):
        prepared = prepare_audit_input(lead_example())
        result = validate_audit_result(example_result(prepared), prepared)
        self.assertEqual(result["commercial_status"], "ACTIVE_INTEREST")
        self.assertEqual(result["review_status"], "PENDING_HUMAN_REVIEW")
        self.assertFalse(result["campaign_eligible"])

    def test_rejects_invented_evidence_and_full_coverage(self):
        prepared = prepare_audit_input(lead_example())
        for overrides in (
            {"evidence_provider_keys": ["does-not-exist"]},
            {"coverage_complete": True},
        ):
            with self.assertRaises(ValueError):
                validate_audit_result(example_result(prepared, **overrides), prepared)

    def test_no_response_not_provable_when_sync_incomplete(self):
        prepared = prepare_audit_input(lead_example())
        with self.assertRaisesRegex(ValueError, "absence_not_proven"):
            validate_audit_result(example_result(
                prepared, commercial_status="NO_EFFECTIVE_CONTACT",
                contact_effective=False,
            ), prepared)

    def test_optout_needs_evidence_and_consistent_state(self):
        prepared = prepare_audit_input(lead_example())
        with self.assertRaises(ValueError):
            validate_audit_result(example_result(
                prepared, commercial_status="DO_NOT_CONTACT",
                opt_out=True, evidence_provider_keys=[],
            ), prepared)
        with self.assertRaises(ValueError):
            validate_audit_result(example_result(
                prepared, commercial_status="ACTIVE_INTEREST",
                opt_out=True,
            ), prepared)

    def test_unknown_with_no_messages_is_allowed(self):
        prepared = prepare_audit_input({"_id": "L2", "messages": []})
        result = validate_audit_result({
            "commercial_status": "UNKNOWN",
            "contact_effective": None,
            "follow_up_required": None,
            "opt_out": None,
            "next_action": None,
            "preferences": {},
            "evidence_provider_keys": [],
            "uncertainty": "No hay mensajes para revisar",
            "coverage_complete": False,
        }, prepared)
        self.assertEqual(result["commercial_status"], "UNKNOWN")

    def test_effective_contact_rejects_only_client_or_only_executive_evidence(self):
        prepared = prepare_audit_input(lead_example())
        for only in ("session:messages.upsert:phone:msg0",
                     "session:messages.upsert:phone:msg1"):
            with self.assertRaisesRegex(ValueError, "bidirectional"):
                validate_audit_result(example_result(
                    prepared, evidence_provider_keys=[only],
                ), prepared)

    def test_effective_contact_requires_same_assignment_cycle(self):
        lead = lead_example()
        lead["executive_whatsapp_messages"][1]["assignment_cycle_id"] = "C2"
        prepared = prepare_audit_input(lead)
        with self.assertRaisesRegex(ValueError, "bidirectional"):
            validate_audit_result(example_result(prepared), prepared)

    def test_reject_integer_flags_as_not_booleans(self):
        prepared = prepare_audit_input(lead_example())
        for key in ("contact_effective", "follow_up_required", "opt_out"):
            with self.assertRaisesRegex(ValueError, "invalid_boolean_field"):
                validate_audit_result(example_result(prepared, **{key: 1}), prepared)

    def test_optout_requires_evidence_from_client_not_executive(self):
        prepared = prepare_audit_input(lead_example())
        with self.assertRaisesRegex(ValueError, "client_evidence"):
            validate_audit_result(example_result(
                prepared, commercial_status="DO_NOT_CONTACT",
                contact_effective=False, opt_out=True,
                evidence_provider_keys=["session:messages.upsert:phone:msg0"],
            ), prepared)
