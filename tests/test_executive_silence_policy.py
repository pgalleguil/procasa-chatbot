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

def decide(lead=None, now="2026-11-25T19:00:00", **overrides):
    args = dict(now=dt(now), enabled_since=dt("2026-11-18T09:00:00"),
                contact_authorized=True, inquiry_verified=True, history_complete=True,
                session_verified=True, followup_attempts=0, outstanding_own_action=False,
                scheduled_contact_clear=True, continuity_since=dt("2026-11-18T09:00:00"))
    args.update(overrides)
    return evaluate_silence(lead if lead is not None else fixture(), **args)

class TestExecutiveSilencePolicy(TestCase):
    def test_wait_and_preferred_evening(self):
        lead = fixture("2026-11-25T12:14:00")
        for now in ("2026-11-25T14:13:00", "2026-11-25T14:14:00",
                    "2026-11-25T17:59:00"):
            planned = decide(lead, now=now)
            self.assertEqual(planned["state"], "WAIT")
            self.assertEqual(planned["due_at"], dt("2026-11-25T18:00:00"))
        self.assertEqual(decide(lead)["state"], "CANDIDATE")
        self.assertEqual(decide(lead)["scenario"], "INITIAL_NO_REPLY")
        self.assertEqual(decide(lead, now="2026-11-25T18:00:00")["state"], "CANDIDATE")
        self.assertEqual(decide(lead, now="2026-11-25T18:29:59")["state"], "CANDIDATE")

    def test_weekend_and_holiday(self):
        lead = fixture("2026-11-20T18:00:00")
        self.assertEqual(decide(lead, now="2026-11-21T19:00:00")["state"], "WAIT")
        self.assertEqual(decide(lead, now="2026-11-23T17:59:00")["state"], "WAIT")
        self.assertEqual(decide(lead, now="2026-11-23T19:00:00")["state"], "CANDIDATE")
        lead = fixture("2026-12-07T18:00:00")
        self.assertEqual(decide(lead, now="2026-12-08T19:00:00")["state"], "WAIT")
        self.assertEqual(decide(lead, now="2026-12-09T19:00:00")["state"], "CANDIDATE")

    def test_outside_send_window(self):
        lead = fixture("2026-11-25T16:00:00")
        self.assertEqual(decide(lead, now="2026-11-25T19:59:00")["state"], "CANDIDATE")
        result = decide(lead, now="2026-11-25T20:00:00")
        self.assertEqual(result["reason"], "outside_send_window")
        self.assertEqual(result["due_at"], dt("2026-11-26T18:00:00"))

    def test_late_anchor(self):
        lead = fixture("2026-11-25T18:00:00")
        self.assertEqual(decide(lead)["state"], "WAIT")
        self.assertEqual(decide(lead, now="2026-11-26T19:00:00")["state"], "CANDIDATE")

    def test_interrupted_dialogue_and_disconnect(self):
        lead = fixture("2026-11-25T16:00:00")
        customer = deepcopy(lead["messages"][0])
        customer.update(message_id="c1", provider_message_id="pc1", role="user",
                        actor_type="customer", source="executive_whatsapp",
                        timestamp=dt("2026-11-25T15:30:00").isoformat(),
                        content="Busco departamento cerca del Metro")
        lead["messages"].insert(0, customer)
        lead["messages"][-1]["content"] = "¿Cuál es su presupuesto?"
        self.assertEqual(decide(lead)["scenario"], "DIALOGUE_INTERRUPTED")
        self.assertEqual(decide(lead, continuity_since=None)["state"], "HOLD")
        self.assertEqual(decide(lead, continuity_since=dt("2026-11-25T17:00:00"))["state"], "HOLD")
        self.assertEqual(decide(lead, continuity_since=dt("2026-11-25T15:45:00"))["state"], "HOLD")
        self.assertEqual(decide(lead, scheduled_contact_clear=False)["state"], "HOLD")
        lead["follow_up_status"] = "pending"
        self.assertEqual(decide(lead)["state"], "HOLD")

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
                     "Ok, gracias", "Yo le aviso", "Más adelante", "No sé ni que es Procasa",
                     "Escríbame mañana", "Contácteme el lunes", "Estoy esperando el crédito"):
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

    def test_contextual_copy(self):
        from chatbot.executive_silence_copy import followup_copy
        first = followup_copy(scenario="INITIAL_NO_REPLY", attempt=1)
        second = followup_copy(scenario="DIALOGUE_INTERRUPTED", attempt=1, last_agent="¿Cuál es su presupuesto?")
        self.assertIn("propiedad por la que consultó", first)
        self.assertIn("presupuesto", second)
        self.assertNotEqual(first, second)
        self.assertIn("No insistiremos", followup_copy(scenario="DIALOGUE_INTERRUPTED", attempt=3))

    def test_verified_operation_copy(self):
        from chatbot.executive_silence_copy import followup_copy
        rental = followup_copy(scenario="INITIAL_NO_REPLY", attempt=1, verified_operation="ARRIENDO")
        sale = followup_copy(scenario="INITIAL_NO_REPLY", attempt=1, verified_operation="VENTA")
        unknown = followup_copy(scenario="INITIAL_NO_REPLY", attempt=1, verified_operation="UNKNOWN")
        self.assertIn("requisitos para postular", rental)
        self.assertIn("proceso de compra", sale)
        self.assertNotIn("postular", unknown)
        self.assertNotIn("proceso de compra", unknown)


def test_rollout_day_start_is_stable_for_all_future_contacts(monkeypatch):
    from chatbot.executive_silence_worker import _rollout_cutoff
    from datetime import datetime, timezone
    monkeypatch.setenv("PROCASA_EXEC_SILENCE_CUTOFF", "2026-11-25T18:14:15+00:00")
    start = datetime(2026, 11, 25, 3, tzinfo=timezone.utc)
    assert _rollout_cutoff(datetime(2026, 11, 25, 22, tzinfo=timezone.utc)) == start
    assert _rollout_cutoff(datetime(2026, 11, 26, 22, tzinfo=timezone.utc)) == start
    assert _rollout_cutoff(datetime(2026, 12, 1, 22, tzinfo=timezone.utc)) == start
    monkeypatch.delenv("PROCASA_EXEC_SILENCE_CUTOFF")
    assert _rollout_cutoff(datetime(2026, 11, 26, 22, tzinfo=timezone.utc)) is None
