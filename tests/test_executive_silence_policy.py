"""Phase one: silence is not consent and a reply is not a resolution."""
from copy import deepcopy
from datetime import datetime, timedelta
from unittest import TestCase

from chatbot.executive_silence_policy import evaluate_silence

def dt(value):
    return datetime.fromisoformat(value + "-03:00")

def fixture(timestamp="2026-11-24T11:00:00"):
    return {
        "stage": "gestion",
        "lifecycle": {"assigned_to_user_id": "owner", "current_assignment_cycle_id": "cycle"},
        "commercial_intelligence": {
            "pending": False, "status": "ready", "pending_session_id": "session",
            "last_processed_cycle_id": "cycle", "last_processed_message_id": "m1",
            "summary": {
                "intent": "NO_SIGNAL", "contact_permission": "UNKNOWN",
                "contact": {"agent_contacted": True, "awaiting": "CUSTOMER"},
                "executive_management": {"pending_customer_request": False},
            },
            "opportunities": [],
        },
        "messages": [{
            "message_id": "m1", "provider_message_id": "p1", "role": "agent",
            "channel": "executive_whatsapp", "source": "crm_manual_api",
            "actor_type": "human_agent", "actor_id": "owner",
            "assignment_cycle_id": "cycle", "assigned_executive_id": "owner",
            "wasender_session_id": "session", "timestamp": dt(timestamp).isoformat(),
            "content": "Buenos días, vi que estaba revisando una propiedad, indíqueme en qué le puedo ayudar.",
        }],
    }

def decide(lead=None, now="2026-11-25T11:00:00", **overrides):
    args = dict(now=dt(now), enabled_since=dt("2026-11-18T09:00:00"),
                contact_authorized=True, inquiry_verified=True, history_complete=True,
                session_verified=True, followup_attempts=0, outstanding_own_action=False)
    args.update(overrides)
    return evaluate_silence(lead if lead is not None else fixture(), **args)

class TestExecutiveSilencePolicy(TestCase):
    def test_wait_one_business_day(self):
        self.assertEqual(decide(now="2026-11-24T18:00:00")["state"], "WAIT")
        self.assertEqual(decide()["state"], "CANDIDATE")

    def test_weekend_and_holiday(self):
        lead = fixture("2026-11-20T17:00:00")
        self.assertEqual(decide(lead, now="2026-11-23T16:59:00")["state"], "WAIT")
        self.assertEqual(decide(lead, now="2026-11-23T17:00:00")["state"], "CANDIDATE")
        # December 8 is a Chilean public holiday.
        lead = fixture("2026-12-07T11:00:00")
        self.assertEqual(decide(lead, now="2026-12-08T12:00:00")["state"], "WAIT")
        self.assertEqual(decide(lead, now="2026-12-09T11:00:00")["state"], "CANDIDATE")

    def test_outside_send_window(self):
        self.assertEqual(decide(now="2026-11-25T18:30:00")["reason"], "outside_send_window")

    def test_missing_authorization_or_attempt(self):
        for key, value in (("contact_authorized", False), ("inquiry_verified", False),
                           ("history_complete", False), ("session_verified", False),
                           ("followup_attempts", None), ("followup_attempts", 1),
                           ("outstanding_own_action", None), ("outstanding_own_action", True)):
            with self.subTest(key=key, value=value):
                self.assertEqual(decide(**{key: value})["state"], "HOLD")

    def test_our_turn_or_pending_request(self):
        for text in ("Voy a revisar en mi cartera y le comento",
                     "Apenas tenga respuesta le escribo",
                     "Indíqueme en qué le puedo ayudar. Revisaré disponibilidad de visita.",
                     "👍", "Gracias", "https://example.invalid/propiedad"):
            lead = fixture()
            lead["messages"][0]["content"] = text
            self.assertEqual(decide(lead)["state"], "HOLD", text)
        lead = fixture()
        lead["commercial_intelligence"]["summary"]["contact"]["awaiting"] = "EXECUTIVE"
        self.assertEqual(decide(lead)["state"], "HOLD")
        lead = fixture()
        lead["commercial_intelligence"]["opportunities"] = [{"status": "PENDIENTE_GESTION"}]
        self.assertEqual(decide(lead)["state"], "HOLD")

    def test_customer_closure_and_deferral(self):
        for text in ("Ya encontré departamento", "No me interesa", "Gracias",
                     "Ok, gracias", "Yo le aviso", "Más adelante", "No sé ni que es Procasa"):
            lead = fixture()
            customer = deepcopy(lead["messages"][0])
            customer.update(message_id="c1", provider_message_id="pc1", role="user",
                            actor_type="customer", source="executive_whatsapp",
                            timestamp=dt("2026-11-24T10:30:00").isoformat(), content=text)
            lead["messages"].insert(0, customer)
            self.assertEqual(decide(lead)["state"], "HOLD", text)

    def test_newer_reply_in_other_channel_and_reassignment(self):
        lead = fixture()
        lead["messages"].append({"role": "user", "timestamp": dt("2026-11-24T12:00:00").isoformat(),
                                 "content": "Sí, me interesa"})
        self.assertEqual(decide(lead)["state"], "HOLD")
        lead = fixture()
        lead["lifecycle"]["current_assignment_cycle_id"] = "new-cycle"
        self.assertEqual(decide(lead)["state"], "HOLD")

    def test_cutoff_and_stale_and_test_leads(self):
        self.assertEqual(decide(enabled_since=dt("2026-11-25T09:00:00"))["state"], "HOLD")
        self.assertEqual(decide(now="2026-12-02T11:00:00")["state"], "HOLD")
        lead = fixture()
        lead["test_scenario"] = "probe"
        self.assertEqual(decide(lead)["state"], "HOLD")

    def test_no_provider_id_no_fresh_summary(self):
        lead = fixture()
        del lead["messages"][0]["provider_message_id"]
        self.assertEqual(decide(lead)["state"], "HOLD")
        lead = fixture()
        lead["commercial_intelligence"]["pending"] = True
        self.assertEqual(decide(lead)["state"], "HOLD")
