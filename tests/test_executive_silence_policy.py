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
        self.assertEqual(result["due_at"], dt("2026-11-26T09:00:00"))

    def test_missed_evening_recovers_next_workday_morning_only(self):
        lead = fixture("2026-11-25T12:14:00")
        for now in ("2026-11-25T20:00:00", "2026-11-25T21:00:02",
                    "2026-11-26T08:59:59"):
            result = decide(lead, now=now)
            self.assertEqual(result["state"], "WAIT")
            self.assertEqual(result["due_at"], dt("2026-11-26T09:00:00"))
        self.assertEqual(decide(lead, now="2026-11-26T09:00:00")["state"], "CANDIDATE")
        self.assertEqual(decide(lead, now="2026-11-26T09:59:59")["state"], "CANDIDATE")
        self.assertEqual(decide(lead, now="2026-11-26T10:00:00")["state"], "WAIT")
        self.assertEqual(decide(lead, now="2026-11-26T09:00:00", contact_authorized=False)["state"], "HOLD")
        self.assertEqual(decide(lead, now="2026-11-26T09:00:00", followup_attempts=1)["state"], "HOLD")
        self.assertEqual(decide(lead, now="2026-11-26T09:00:00", scheduled_contact_clear=False)["state"], "HOLD")

    def test_missed_friday_and_holiday_recover_at_business_open(self):
        friday = fixture("2026-11-20T12:00:00")
        self.assertEqual(decide(friday, now="2026-11-20T20:00:00")["due_at"], dt("2026-11-23T09:00:00"))
        self.assertEqual(decide(friday, now="2026-11-21T09:00:00")["state"], "WAIT")
        self.assertEqual(decide(friday, now="2026-11-23T09:00:00")["state"], "CANDIDATE")
        before_holiday = fixture("2026-12-07T12:00:00")
        self.assertEqual(decide(before_holiday, now="2026-12-07T20:00:00")["due_at"], dt("2026-12-09T09:00:00"))
        self.assertEqual(decide(before_holiday, now="2026-12-08T09:00:00")["state"], "WAIT")
        self.assertEqual(decide(before_holiday, now="2026-12-09T09:00:00")["state"], "CANDIDATE")

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


def test_enqueue_all_executives_and_cancel_reply(monkeypatch):
    from chatbot.executive_silence_worker import enqueue
    from unittest.mock import MagicMock
    monkeypatch.setenv("PROCASA_EXEC_SILENCE_WORKER_ENABLED", "1")
    monkeypatch.setenv("PROCASA_EXEC_SILENCE_CUTOFF", "2026-11-25T18:14:15+00:00")
    db = MagicMock()
    leads, queue = MagicMock(), MagicMock()
    db.__getitem__.side_effect = lambda k: {"leads": leads, "crm_exec_silence_queue": queue}[k]
    for owner in ("owner-A", "owner-B"):
        lead = fixture("2026-11-26T12:00:00")
        lead["lifecycle"]["assigned_to_user_id"] = owner
        lead["commercial_intelligence"]["summary"]["contact"].update(
            last_actor="human_agent", last_agent_at="2026-11-26T12:00:00-03:00")
        leads.find_one.return_value = lead
        enqueue(db, owner, now=dt("2026-11-26T15:00:00"))
        job = queue.update_one.call_args.args[1]["$setOnInsert"]
        assert job["owner"] == owner
        assert job["due_at"] == dt("2026-11-26T18:00:00")
        queue.update_one.reset_mock()
        lead["commercial_intelligence"]["summary"]["contact"].update(
            last_actor="customer", awaiting="EXECUTIVE")
        enqueue(db, owner, now=dt("2026-11-26T16:00:00"))
        queue.update_one.assert_not_called()
        assert queue.update_many.call_args.args[0] == {"lead_id": owner, "state": "pending"}


def test_waiting_response_is_not_a_scheduled_commitment():
    lead = fixture("2026-11-25T12:22:00")
    lead.update(follow_up_required=True, follow_up_status="pending",
                management_status="managed_waiting_response",
                contact_attempted=True, effective_contact=False)
    assert decide(lead)["state"] == "CANDIDATE"
    assert decide(lead, scheduled_contact_clear=False)["state"] == "HOLD"
    assert decide(lead, contact_authorized=False)["state"] == "HOLD"
    lead["management_status"] = "managed_contacted"
    assert decide(lead)["reason"] == "crm_followup_pending"


def test_automatic_contact_evidence_without_manual_pilot_approval(monkeypatch):
    from chatbot.executive_silence_worker import _contact_evidence
    from chatbot import crm_hot_linked_auto
    from unittest.mock import MagicMock
    monkeypatch.setattr(crm_hot_linked_auto, "_verified_whatsapp_permission", lambda *a, **kw: False)
    monkeypatch.setattr(crm_hot_linked_auto, "_verified_recent_office_job", lambda *a, **kw: False)
    db = MagicMock()
    doc = fixture("2026-11-25T12:22:00")
    job = {"cycle": "cycle", "owner": "owner", "session": "session", "anchor": "m1"}
    now = dt("2026-11-25T19:00:00")
    phone = "+56912345678"
    assert not _contact_evidence(db, doc, job, phone, now)
    customer = deepcopy(doc["messages"][0])
    customer.update(actor_type="customer", role="user", timestamp=dt("2026-11-25T12:00:00").isoformat())
    doc["messages"].insert(0, customer)
    assert _contact_evidence(db, doc, job, phone, now)
    customer["wasender_session_id"] = "other"
    assert not _contact_evidence(db, doc, job, phone, now)
    monkeypatch.setattr(crm_hot_linked_auto, "_verified_recent_office_job", lambda *a, **kw: True)
    assert _contact_evidence(db, doc, job, phone, now)
    assert "silence_pilot_review" not in doc["commercial_intelligence"]


def test_session_guard_serializes_and_pauses_on_rate_limit(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    from pymongo.errors import DuplicateKeyError
    db, guard = MagicMock(), MagicMock()
    db.__getitem__.return_value = guard
    job = {"session": "133517"}
    now = dt("2026-11-25T19:00:00")
    monkeypatch.setattr(worker, "_live_allowed", lambda job: True)
    send = MagicMock(return_value="accepted")
    monkeypatch.setattr(worker, "_dispatch_locked", send)
    assert worker.dispatch(db, job, {}, {}, now=now) == "accepted"
    guard.delete_one.assert_called_once()
    guard.delete_one.reset_mock()
    guard.insert_one.side_effect = DuplicateKeyError("already locked")
    guard.find_one.return_value = {"state": "sending"}
    assert worker.dispatch(db, job, {}, {}, now=now) == "session_busy"
    send.assert_called_once()
    guard.insert_one.side_effect = None
    send.return_value = "rate_limited"
    assert worker.dispatch(db, job, {}, {}, now=now) == "rate_limited"
    guard.delete_one.assert_not_called()
    assert guard.update_one.call_args.args[1]["$set"]["state"] == "paused"
    guard.insert_one.side_effect = DuplicateKeyError("paused")
    guard.find_one.return_value = {"state": "paused"}
    assert worker.dispatch(db, job, {}, {}, now=now) == "session_paused"
    assert send.call_count == 2


def test_manual_portal_inquiry_accepted_first_contact(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    doc = fixture("2026-11-25T12:22:00")
    doc.update(_id="lead", source_type="manual", prospecto={"codigo": "5965", "canal_origen": "Enlace Inmobiliario"})
    job = {"cycle": "cycle", "owner": "owner", "session": "session", "anchor": "m1"}
    db, outbox = MagicMock(), MagicMock()
    db.__getitem__.return_value = outbox
    now = dt("2026-11-25T19:00:00")
    outbox.find_one.return_value = {"_id": "accepted"}
    assert worker._verified_portal_first_contact(db, doc, job, now)
    query = outbox.find_one.call_args.args[0]
    assert query["state"] == "accepted" and query["canonical_recorded"] is True
    assert query["session_id"] == "session" and query["provider_message_id"] == "p1"
    outbox.find_one.return_value = None
    assert not worker._verified_portal_first_contact(db, doc, job, now)
    doc["messages"][0]["wasender_session_id"] = "other"
    outbox.find_one.return_value = {"_id": "accepted"}
    assert not worker._verified_portal_first_contact(db, doc, job, now)
    doc["messages"][0]["wasender_session_id"] = "session"
    doc["prospecto"]["canal_origen"] = "unknown"
    assert not worker._verified_portal_first_contact(db, doc, job, now)


def test_provider_log_bootstrap_requires_history_covering_anchor():
    from datetime import datetime, timezone
    from chatbot.executive_capture_continuity import _provider_connected_epoch
    anchor = datetime(2026, 11, 25, 15, 22, tzinfo=timezone.utc)
    now = datetime(2026, 11, 25, 22, 50, tzinfo=timezone.utc)
    before = {"whatsapp_session_id": 133517, "event_type": "status_change",
              "status": "connected", "occurred_at": "2026-11-25T13:00:00Z"}
    after = {**before, "occurred_at": "2026-11-25T18:00:00Z"}
    assert _provider_connected_epoch([before], session_id="133517", anchor=anchor, now=now) == datetime(2026, 11, 25, 13, tzinfo=timezone.utc)
    assert _provider_connected_epoch([after], session_id="133517", anchor=anchor, now=now) is None
    for bad in ({**after, "status": "disconnected"}, {**after, "event_type": "session_restarted"}, {**after, "whatsapp_session_id": 123}):
        assert _provider_connected_epoch([bad, before], session_id="133517", anchor=anchor, now=now) is None


def test_exceptional_recovery_window_is_bounded_and_keeps_guards():
    lead = fixture("2026-11-25T12:14:00")
    end = dt("2026-11-25T21:00:00")
    assert decide(lead, now="2026-11-25T20:30:00")["state"] == "WAIT"
    assert decide(lead, now="2026-11-25T20:30:00", recovery_until=end)["state"] == "CANDIDATE"
    assert decide(lead, now="2026-11-25T21:00:00", recovery_until=end)["state"] == "WAIT"
    assert decide(lead, now="2026-11-26T20:30:00", recovery_until=end)["state"] == "WAIT"
    assert decide(lead, now="2026-11-25T20:30:00", recovery_until=end, continuity_since=None)["state"] == "HOLD"
    assert decide(lead, now="2026-11-25T20:30:00", recovery_until=end, followup_attempts=1)["state"] == "HOLD"
    assert decide(lead, now="2026-11-25T20:30:00", recovery_until=end, contact_authorized=False)["state"] == "HOLD"


def test_exceptional_recovery_job_scope():
    from chatbot.executive_silence_worker import _recovery_until, _RECOVERY_JOB_IDS
    assert len(_RECOVERY_JOB_IDS) == 6
    job = {"_id": next(iter(_RECOVERY_JOB_IDS)), "session": "133517", "owner": "6a681413140190dde11f26d1"}
    assert _recovery_until(job) == dt("2026-11-25T21:00:00")
    assert _recovery_until(dict(job, _id="other")) is None
    assert _recovery_until(dict(job, session="other")) is None
    assert _recovery_until(dict(job, owner="other")) is None


def test_provider_recovery_uses_verified_session_log_key_on_auth_rejection(monkeypatch):
    from chatbot import executive_capture_continuity as continuity
    from chatbot import executive_whatsapp_audit as audit
    from config import Config
    from unittest.mock import MagicMock
    monkeypatch.setattr(Config, "WASENDER_PERSONAL_ACCESS_TOKEN", "mock-pat")
    monkeypatch.setattr(audit, "_session", lambda db, sid: {
        "user_id": "owner", "capture_enabled": True, "secret": "mock-secret", "token": "mock-session"
    })
    db, coll = MagicMock(), MagicMock()
    db.__getitem__.return_value = coll
    coll.find_one.side_effect = [None, {"connected_since": dt("2026-11-25T10:00:00")}]
    detail, rejected, logs = MagicMock(), MagicMock(), MagicMock()
    detail.json.return_value = {"success": True, "data": {"id": 133517,
        "status": "connected", "webhook_enabled": True,
        "webhook_events": ["messages.upsert"],
        "webhook_url": "https://example.invalid/webhook/ejecutivos/133517",
        "webhook_secret": "mock-secret"}}
    rejected.status_code = 403
    logs.json.return_value = {"success": True, "data": {"current_page": 1, "data": [{
        "whatsapp_session_id": 133517, "event_type": "status_change",
        "status": "connected", "occurred_at": "2026-11-25T13:00:00Z"}]}}
    get = MagicMock(side_effect=[detail, rejected, logs])
    assert continuity.recover_provider_continuity(db, session_id="133517", owner_id="owner",
        anchor=dt("2026-11-25T12:14:00"), now=dt("2026-11-25T20:00:00"), get=get) == dt("2026-11-25T10:00:00")
    assert get.call_count == 3
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer mock-session"
    assert coll.update_one.call_count == 1


def test_verified_initial_recovery_copy_matches_six_real_property_shapes(monkeypatch):
    from chatbot import executive_silence_llm as llm
    cases = [
        ("Departamento", "Viña del Mar", ["VENTA"]),
        ("Departamento", "Rancagua", ["VENTA", "ARRIENDO"]),
        ("Casa", "San Bernardo", ["VENTA"]),
        ("Casa", "Chillán", ["VENTA"]),
        ("Casa", "Maipú", ["VENTA"]),
        ("Departamento", "Valparaíso", ["VENTA"]),
    ]
    for kind, commune, ops in cases:
        monkeypatch.setattr(llm, "property_context", lambda *a: {
            "tipo": kind, "comuna": commune, "operaciones_publicadas": ops,
            "orientacion_comercial": False})
        text = llm.initial_recovery_draft(None, {}, {})
        assert text.startswith("Hola, por su consulta sobre ")
        assert not text.endswith("— PROCASA")
        assert "seguimiento automático" not in text
        assert "a el" not in text
        assert commune in text and kind.lower() in text
        assert text.count("?") == 1
        assert ("comprar o arrendar" in text) == (len(ops) == 2)
        assert ("financiamiento" in text) == (len(ops) == 1)
    monkeypatch.setattr(llm, "property_context", lambda *a: {})
    assert llm.initial_recovery_draft(None, {}, {}) is None


def test_explicit_model_hold_never_uses_recovery_copy(monkeypatch):
    from chatbot import executive_silence_llm as llm
    monkeypatch.setattr(llm, "build_context", lambda *a: {"turns": [{}]})
    text, usage = llm.draft_followup(None, {}, {}, {}, scenario="INITIAL_NO_REPLY",
        propose=lambda ctx: {"decision": "HOLD", "message": "unexpected"})
    assert text is None


def test_model_value_error_before_provider_does_not_pause_other_jobs(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    from datetime import datetime as RealDateTime
    class FixedDateTime(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            return dt("2026-11-25T20:25:00").astimezone(tz)
    monkeypatch.setattr(worker, "datetime", FixedDateTime)
    monkeypatch.setattr(worker, "_live_allowed", lambda job: True)
    monkeypatch.setattr(worker, "eligible_manual_session", lambda *a, **k: {
        "session": {"session_id": "133517", "user_oid": "owner"}})
    monkeypatch.setattr(worker, "verify_provider_sender_identity", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_session_status", lambda *a: "connected")
    doc = {"messages": [{"message_id": "m1"}]}
    decision = {"state": "CANDIDATE", "scenario": "INITIAL_NO_REPLY"}
    monkeypatch.setattr(worker, "_decision", lambda *a, **k: (doc, decision))
    draft = MagicMock(side_effect=ValueError("draft_format"))
    monkeypatch.setattr(worker, "draft_followup", draft)
    fallback = MagicMock(return_value=None)
    monkeypatch.setattr(worker, "initial_recovery_draft", fallback)
    db, collection, post = MagicMock(), MagicMock(), MagicMock()
    db.__getitem__.return_value = collection
    collection.find_one.return_value = {}
    job = {"_id": "unrelated", "owner": "owner", "session": "133517",
           "anchor": "m1", "lease": "lease"}
    assert worker.dispatch(db, job, doc, decision, now=dt("2026-11-25T20:25:00"), post=post) == "retry_draft_before_provider"
    fallback.assert_not_called()
    post.assert_not_called()
    collection.delete_one.assert_called_once()
    assert collection.update_one.call_args.args[1]["$set"]["draft_error"] == "draft_format"
    job.update(_id=next(iter(worker._RECOVERY_JOB_IDS)), owner="6a681413140190dde11f26d1")
    assert worker.dispatch(db, job, doc, decision, now=dt("2026-11-25T20:25:00"), post=post) == "model_invalid"
    fallback.assert_called_once()
    post.assert_not_called()


def test_reviewed_initial_recovery_requires_explicit_scope_and_single_verified_outbound(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    draft = MagicMock(return_value="verified copy")
    monkeypatch.setattr(worker, "initial_recovery_draft", draft)
    anchor = {"actor_type": "human_agent", "source": "crm_manual_api",
              "message_id": "m1", "provider_echo_message_id": "p1", "provider_echo_at": "stamp"}
    doc = {"messages": [anchor]}
    job = {"_id": next(iter(worker._RECOVERY_JOB_IDS)), "owner": "6a681413140190dde11f26d1",
           "session": "133517", "anchor": "m1", "reviewed_initial_copy": True, "draft_decision": "HOLD"}
    now = dt("2026-11-25T20:40:00")
    decision = {"state": "CANDIDATE", "scenario": "INITIAL_NO_REPLY"}
    run = worker._reviewed_initial_recovery
    assert run(None, job, doc, anchor, decision, now) == "verified copy"
    for bad in (dict(job, reviewed_initial_copy=False), dict(job, _id="other"),
                dict(job, draft_decision="DRAFT"), dict(job, anchor="other")):
        assert run(None, bad, doc, anchor, decision, now) is None
    assert run(None, job, {"messages": [anchor, {"actor_type": "customer"}]}, anchor, decision, now) is None
    assert run(None, job, doc, anchor, dict(decision, state="HOLD"), now) is None
    assert run(None, job, doc, anchor, dict(decision, scenario="DIALOGUE_INTERRUPTED"), now) is None
    assert run(None, job, doc, anchor, decision, dt("2026-11-25T21:00:00")) is None
    assert draft.call_count == 1


def test_contextual_draft_uses_natural_copy_without_automated_prefix():
    from chatbot import executive_silence_llm as llm
    msg = "Hola, por su consulta sobre el departamento en Rancagua, ¿está buscando comprar o arrendar? Así podemos orientarle mejor."
    assert llm.validate_draft({"decision": "DRAFT", "message": msg}) == msg
    assert llm.validate_draft({"decision": "DRAFT", "message": msg + " — PROCASA"}) == msg
    msg = "Desde PROCASA podemos orientarle. ¿Prefiere revisar otras opciones?"
    assert llm.validate_draft({"decision": "DRAFT", "message": msg}) == msg
    for bad in ("Hola, seguimiento automático de PROCASA. ¿Busca comprar?",
                "Respecto a el departamento, ¿busca comprar?",
                "Hola, soy Hernán. ¿Busca comprar?"):
        try:
            llm.validate_draft({"decision": "DRAFT", "message": bad})
        except ValueError:
            pass
        else:
            raise AssertionError("Unacceptable copy passed validation")


def test_contextual_draft_resumes_dialogue_not_repeats_opening(monkeypatch):
    from chatbot import executive_silence_llm as llm
    context = {"scenario": "DIALOGUE_INTERRUPTED", "turns": [
        {"actor": "CLIENTE", "texto": "Busco arriendo cerca del Metro"},
        {"actor": "EJECUTIVO", "texto": "Le compartimos alternativas"}]}
    monkeypatch.setattr(llm, "build_context", lambda *a: context)
    def propose(ctx):
        assert ctx is context
        return {"decision": "DRAFT", "message": "¿Alguna de las propiedades que le compartimos se acerca a lo que busca o prefiere que revisemos otras opciones?"}
    text, usage = llm.draft_followup(None, {}, {}, {}, scenario="DIALOGUE_INTERRUPTED", propose=propose)
    assert text.startswith("¿Alguna de las propiedades")
    assert not text.endswith("— PROCASA")
    assert usage["version"] == llm.VERSION


def test_retry_window_before_provider_is_pending_not_terminal(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    monkeypatch.setattr(worker, "enabled", lambda: True)
    db, queue = MagicMock(), MagicMock()
    db.__getitem__.return_value = queue
    job = {"_id": "job", "lease": "lease", "session": "s"}
    queue.find_one_and_update.side_effect = [job, None]
    monkeypatch.setattr(worker, "_decision", lambda *a, **k: ({}, {"state": "CANDIDATE", "reason": "customer_reply_overdue"}))
    monkeypatch.setattr(worker, "dispatch", lambda *a, **k: "retry_window_before_provider")
    worker.process_due(db, now=dt("2026-11-25T19:59:00"))
    update = queue.update_one.call_args.args[1]["$set"]
    assert update["state"] == "pending"
    assert update["reason"] == "retry_window_before_provider"


def test_retry_draft_before_provider_is_bounded_and_pending(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    monkeypatch.setattr(worker, "enabled", lambda: True)
    db, queue = MagicMock(), MagicMock()
    db.__getitem__.return_value = queue
    queue.find_one_and_update.side_effect = [{"_id": "job", "lease": "lease", "session": "s"}, None]
    monkeypatch.setattr(worker, "_decision", lambda *a, **k: ({}, {"state": "CANDIDATE", "reason": "customer_reply_overdue"}))
    monkeypatch.setattr(worker, "dispatch", lambda *a, **k: "retry_draft_before_provider")
    worker.process_due(db, now=dt("2026-11-25T19:00:00"))
    update = queue.update_one.call_args.args[1]["$set"]
    assert update["state"] == "pending"
    assert update["draft_retry_count"] == 1


def test_initial_silence_excludes_any_previous_executive_dialogue(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    db, leads = MagicMock(), MagicMock()
    db.__getitem__.return_value = leads
    lead = fixture("2026-11-25T12:00:00")
    job = {"owner": "owner", "cycle": "cycle", "session": "session",
           "anchor": "m1", "lead_id": "lead"}
    assert not worker._has_executive_dialogue(lead)
    lead["messages"].insert(0, {"channel": "chatbot", "role": "user", "actor_type": "customer"})
    assert not worker._has_executive_dialogue(lead)
    for customer in (
        {"channel": "executive_whatsapp", "actor_type": "customer", "assignment_cycle_id": "cycle"},
        {"channel": "executive_whatsapp", "actor_type": "customer", "assignment_cycle_id": "prior_cycle"},
        {"channel": "executive_whatsapp", "role": "user"},
    ):
        doc = deepcopy(lead)
        doc["messages"].insert(0, customer)
        assert worker._has_executive_dialogue(doc)
        leads.find_one.return_value = doc
        _, decision = worker._decision(db, job, now=dt("2026-11-25T19:00:00"))
        assert decision == {"state": "HOLD", "reason": "dialogue_policy_pending",
                            "scenario": "DIALOGUE_INTERRUPTED"}
    sender = MagicMock()
    assert worker.dispatch(db, job, lead, {"scenario": "DIALOGUE_INTERRUPTED"},
                           now=dt("2026-11-25T19:00:00"), post=sender) == "dialogue_policy_pending"
    sender.assert_not_called()


def test_dialogue_is_held_for_separate_policy_not_sent(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    monkeypatch.setattr(worker, "enabled", lambda: True)
    db, queue = MagicMock(), MagicMock()
    db.__getitem__.return_value = queue
    queue.find_one_and_update.side_effect = [{"_id": "job", "lease": "lease", "session": "s"}, None]
    monkeypatch.setattr(worker, "_decision", lambda *a, **k: ({}, {
        "state": "HOLD", "reason": "dialogue_policy_pending", "scenario": "DIALOGUE_INTERRUPTED"}))
    send = MagicMock()
    monkeypatch.setattr(worker, "dispatch", send)
    worker.process_due(db, now=dt("2026-11-25T19:00:00"))
    send.assert_not_called()
    update = queue.update_one.call_args.args[1]["$set"]
    assert update["state"] == "held"
    assert update["reason"] == "dialogue_policy_pending"


def test_initial_three_stage_calendar_and_guardrails():
    from chatbot.executive_silence_policy import sequence_due
    lead = fixture("2026-11-25T12:00:00")
    first = dt("2026-11-25T19:00:00")
    assert sequence_due(first, 2) == dt("2026-11-26T12:30:00")
    assert sequence_due(dt("2026-11-26T12:30:00"), 3) == dt("2026-11-30T18:00:00")
    assert sequence_due(dt("2026-12-07T19:00:00"), 2) == dt("2026-12-09T12:30:00")
    lead["messages"].append({"message_id": "auto1", "role": "assistant",
                             "actor_type": "automation", "automatic": True,
                             "source": "crm_exec_silence_api", "timestamp": first.isoformat()})
    prior = ({"message_id": "auto1", "accepted_at": first},)
    opts = dict(followup_attempts=1, verified_followups=prior)
    assert decide(lead, now="2026-11-26T09:00:00", **opts)["state"] == "WAIT"
    assert decide(lead, now="2026-11-26T12:30:00", **opts)["attempt"] == 2
    assert decide(lead, now="2026-11-26T14:00:00", **opts)["due_at"] == dt("2026-11-27T09:00:00")
    assert decide(lead, now="2026-11-27T09:00:00", **opts)["state"] == "CANDIDATE"
    assert decide(lead, now="2026-11-26T12:30:00", **opts, contact_authorized=False)["state"] == "HOLD"
    assert decide(lead, now="2026-11-26T12:30:00", **opts, scheduled_contact_clear=False)["state"] == "HOLD"
    second = dt("2026-11-26T12:30:00")
    lead["messages"].append({"message_id": "auto2", "role": "assistant",
                             "actor_type": "automation", "automatic": True,
                             "source": "crm_exec_silence_api", "timestamp": second.isoformat()})
    opts = dict(followup_attempts=2, verified_followups=prior + ({"message_id": "auto2", "accepted_at": second},))
    assert decide(lead, now="2026-11-30T18:00:00", **opts)["attempt"] == 3
    assert decide(lead, now="2026-11-30T18:00:00", followup_attempts=3)["state"] == "HOLD"
    lead["messages"].append({"role": "user", "actor_type": "customer",
                             "channel": "executive_whatsapp", "timestamp": dt("2026-11-27T12:00:00").isoformat()})
    assert decide(lead, now="2026-11-30T18:00:00", **opts)["state"] == "HOLD"


def test_sequence_history_requires_exact_provider_and_canonical_proof():
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    from hashlib import sha256
    phone = "+56912345678"
    key = worker._attempt_key(phone, 1)
    assert key == sha256(f"{worker.MODE}|{phone}".encode()).hexdigest()
    assert len({worker._attempt_key(phone, n) for n in (1, 2, 3)}) == 3
    job = {"attempt": 2, "owner": "owner", "cycle": "cycle", "session": "session",
           "anchor": "m1", "lead_id": "lead"}
    lead = fixture()
    lead["_id"] = "lead"
    text = "¿Sigue interesado en la propiedad?"
    row = {"_id": key, "state": "accepted", "canonical_recorded": True,
           "lead_id": "lead", "executive_id": "owner", "assignment_cycle_id": "cycle",
           "session_id": "session", "anchor_message_id": "m1", "scenario": "INITIAL_NO_REPLY",
           "provider_message_id": "p2", "provider_result_at": dt("2026-11-25T19:00:00"),
           "content_sha256": sha256(text.encode()).hexdigest()}
    auto = {"message_id": "auto", "silence_dispatch_id": key, "source": worker.MODE,
            "actor_type": "automation", "automatic": True, "assigned_executive_id": "owner",
            "assignment_cycle_id": "cycle", "wasender_session_id": "session",
            "provider_message_id": "p2", "provider_echo_message_id": "echo", "content": text}
    lead["messages"].append(auto)
    db, outbox = MagicMock(), MagicMock()
    db.__getitem__.return_value = outbox
    outbox.find.return_value.limit.return_value = [row]
    assert worker._sequence_evidence(db, lead, job, phone)[0]["message_id"] == "auto"
    for patch in ({"state": "delivery_unknown"}, {"canonical_recorded": False},
                  {"executive_id": "other"}, {"anchor_message_id": "other"}, {"provider_message_id": "other"}):
        outbox.find.return_value.limit.return_value = [dict(row, **patch)]
        assert worker._sequence_evidence(db, lead, job, phone) is None
    outbox.find.return_value.limit.return_value = [row]
    auto.pop("provider_echo_message_id")
    assert worker._sequence_evidence(db, lead, job, phone) is None


def test_successor_is_idempotent_and_never_same_day():
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    db, outbox, queue = MagicMock(), MagicMock(), MagicMock()
    db.__getitem__.side_effect = lambda k: {worker.OUTBOX: outbox, worker.QUEUE: queue}[k]
    lead = fixture()
    lead.update(_id="lead", phone="+56912345678")
    job = {"_id": "root", "lead_id": "lead", "owner": "owner", "cycle": "cycle",
           "session": "session", "anchor": "m1", "attempt": 1}
    outbox.find_one.return_value = {"provider_result_at": dt("2026-11-26T09:01:00")}
    worker._schedule_successor(db, job, lead)
    worker._schedule_successor(db, job, lead)
    assert queue.update_one.call_args_list[0] == queue.update_one.call_args_list[1]
    next_job = queue.update_one.call_args.args[1]["$setOnInsert"]
    assert next_job["attempt"] == 2
    assert next_job["due_at"] == dt("2026-11-27T12:30:00")
    queue.reset_mock()
    worker._schedule_successor(db, dict(job, attempt=3), lead)
    queue.update_one.assert_not_called()
    lead["messages"].append({"channel": "executive_whatsapp", "actor_type": "customer"})
    worker._schedule_successor(db, job, lead)
    queue.update_one.assert_not_called()


def test_reconcile_only_durable_accepted_stages_after_crash(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    monkeypatch.setattr(worker, "_last_sequence_reconcile", None)
    db, queue, outbox, leads = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    db.__getitem__.side_effect = lambda k: {worker.QUEUE: queue, worker.OUTBOX: outbox, "leads": leads}[k]
    job = {"_id": "root", "lead_id": "lead", "state": "evaluating",
           "claimed_at": dt("2026-11-25T19:00:00"), "owner": "owner",
           "cycle": "cycle", "session": "session", "anchor": "m1"}
    queue.find.return_value.limit.return_value = [job]
    lead = fixture()
    lead.update(_id="lead", phone="+56912345678")
    leads.find_one.return_value = lead
    successor = MagicMock()
    monkeypatch.setattr(worker, "_schedule_successor", successor)
    outbox.find_one.return_value = None
    worker._reconcile_accepted_sequences(db, dt("2026-11-25T19:20:00"))
    successor.assert_not_called()
    queue.update_one.assert_not_called()
    monkeypatch.setattr(worker, "_last_sequence_reconcile", None)
    outbox.find_one.return_value = {"provider_message_id": "p2",
                                    "provider_result_at": dt("2026-11-25T19:01:00")}
    worker._reconcile_accepted_sequences(db, dt("2026-11-25T19:20:00"))
    successor.assert_called_once_with(db, job, lead)
    assert queue.update_one.call_args.args[1]["$set"]["state"] == "accepted"


def test_operational_incident_is_deduplicated():
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    db, incidents = MagicMock(), MagicMock()
    db.__getitem__.return_value = incidents
    job = {"_id": "job", "lead_id": "lead", "owner": "owner", "session": "s", "attempt": 2}
    worker._record_incident(db, job, "model_hold")
    worker._record_incident(db, job, "model_hold")
    assert incidents.update_one.call_args_list[0].args[0] == incidents.update_one.call_args_list[1].args[0]
    assert incidents.update_one.call_args.args[1]["$setOnInsert"]["attempt"] == 2


def test_stale_before_provider_lease_recovered_only_once(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    monkeypatch.setattr(worker, "_last_sequence_reconcile", None)
    db, queue, outbox, leads, guard = [MagicMock() for _ in range(5)]
    db.__getitem__.side_effect = lambda k: {worker.QUEUE: queue, worker.OUTBOX: outbox,
        "leads": leads, worker.SESSION_GUARD: guard}[k]
    job = {"_id": "root", "lead_id": "lead", "state": "evaluating",
           "phase": "pre_provider", "lease": "old-lease",
           "claimed_at": dt("2026-11-25T19:00:00"), "owner": "owner",
           "cycle": "cycle", "session": "session", "anchor": "m1"}
    queue.find.return_value.limit.return_value = [job]
    lead = fixture()
    lead.update(_id="lead", phone="+56912345678")
    leads.find_one.return_value = lead
    outbox.find_one.return_value = None
    queue.update_one.return_value.modified_count = 1
    incident = MagicMock()
    monkeypatch.setattr(worker, "_record_incident", incident)
    worker._reconcile_accepted_sequences(db, dt("2026-11-25T19:20:00"))
    assert queue.update_one.call_args.args[0]["lease"] == "old-lease"
    assert queue.update_one.call_args.args[1]["$set"]["state"] == "pending"
    assert queue.update_one.call_args.args[1]["$set"]["pre_provider_recoveries"] == 1
    guard.delete_one.assert_called_once_with({"_id": "session", "job_id": "root",
                                             "lease": "old-lease", "state": "sending"})
    queue.reset_mock()
    guard.reset_mock()
    monkeypatch.setattr(worker, "_last_sequence_reconcile", None)
    queue.find.return_value.limit.return_value = [dict(job, phase="reservation_pending")]
    worker._reconcile_accepted_sequences(db, dt("2026-11-25T19:20:00"))
    queue.update_one.assert_not_called()
    guard.delete_one.assert_not_called()
    incident.assert_called_once()


def test_expired_authorization_cannot_be_replaced_by_old_portal_evidence(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    db, leads, tasks = MagicMock(), MagicMock(), MagicMock()
    db.__getitem__.side_effect = lambda k: {"leads": leads, "crm_tasks": tasks}[k]
    lead = fixture()
    lead.update(_id="lead", phone="+56912345678")
    lead["crm_hot_auto"] = {"whatsapp_contact_authorization": {
        "status": "ACTIVE", "expires_at": "2026-11-25T18:00:00-03:00"}}
    leads.find_one.return_value = lead
    tasks.find_one.return_value = None
    monkeypatch.setattr(worker, "_sequence_evidence", lambda *a: ())
    evidence = MagicMock(return_value=True)
    monkeypatch.setattr(worker, "_contact_evidence", evidence)
    job = {"lead_id": "lead", "owner": "owner", "cycle": "cycle",
           "session": "session", "anchor": "m1"}
    _, decision = worker._decision(db, job, now=dt("2026-11-26T09:00:00"))
    assert decision["reason"] == "contact_authorization_expired_or_invalid"
    evidence.assert_not_called()


def test_classification_pending_retries_once_then_records_incident(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    db, queue = MagicMock(), MagicMock()
    db.__getitem__.return_value = queue
    monkeypatch.setattr(worker, "enabled", lambda: True)
    monkeypatch.setattr(worker, "_reconcile_accepted_sequences", lambda *a: None)
    monkeypatch.setattr(worker, "_decision", lambda *a, **kw: ({}, {
        "state": "HOLD", "reason": "classification_not_ready"}))
    incident = MagicMock()
    monkeypatch.setattr(worker, "_record_incident", incident)
    job = {"_id": "job", "lease": "lease", "session": "s"}
    queue.find_one_and_update.side_effect = [job, None]
    worker.process_due(db, now=dt("2026-11-26T09:00:00"))
    assert queue.update_one.call_args.args[1]["$set"]["state"] == "pending"
    assert queue.update_one.call_args.args[1]["$set"]["classification_retry_count"] == 1
    incident.assert_not_called()
    queue.find_one_and_update.side_effect = [dict(job, classification_retry_count=1), None]
    worker.process_due(db, now=dt("2026-11-26T09:01:00"))
    assert queue.update_one.call_args.args[1]["$set"]["state"] == "held"
    incident.assert_called_once()


def test_stage_fallback_uses_verified_property_and_distinct_messages(monkeypatch):
    from chatbot import executive_silence_llm as llm
    monkeypatch.setattr(llm, "property_context", lambda *a: {
        "tipo": "Casa", "comuna": "San Bernardo",
        "operaciones_publicadas": ["VENTA"], "orientacion_comercial": False})
    texts = [llm.verified_initial_stage_fallback(None, {}, {}, attempt=n) for n in (1, 2, 3)]
    assert len(set(texts)) == 3
    assert all("San Bernardo" in t and t.count("?") <= 1 for t in texts)
    assert "cuénteme" in texts[0]
    assert "financiamiento" not in texts[0]
    assert "otras propiedades" in texts[1]
    assert "no insistiremos" in texts[2]
    assert all("seguimiento automático" not in t and not t.endswith("— PROCASA") for t in texts)
    monkeypatch.setattr(llm, "property_context", lambda *a: {})
    assert llm.verified_initial_stage_fallback(None, {}, {}, attempt=1) is None


def test_permanent_fallback_after_draft_truncation_not_for_explicit_hold(monkeypatch):
    from chatbot import executive_silence_worker as worker
    from unittest.mock import MagicMock
    db, collection, post = MagicMock(), MagicMock(), MagicMock()
    db.__getitem__.return_value = collection
    collection.find_one.return_value = {}
    doc = {"messages": [{"message_id": "m1"}]}
    decision = {"state": "CANDIDATE", "scenario": "INITIAL_NO_REPLY"}
    job = {"_id": "new", "owner": "owner", "session": "133517",
           "anchor": "m1", "lease": "lease", "draft_retry_count": 1}
    monkeypatch.setattr(worker, "_live_allowed", lambda j: True)
    monkeypatch.setattr(worker, "eligible_manual_session", lambda *a, **k: {"session": {"session_id": "133517", "user_oid": "owner"}})
    monkeypatch.setattr(worker, "verify_provider_sender_identity", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_session_status", lambda *a: "connected")
    monkeypatch.setattr(worker, "_decision", lambda *a, **k: (doc, decision))
    draft = MagicMock(side_effect=ValueError("draft_incomplete"))
    monkeypatch.setattr(worker, "draft_followup", draft)
    fallback = MagicMock(return_value=None)
    monkeypatch.setattr(worker, "verified_initial_stage_fallback", fallback)
    assert worker.dispatch(db, job, doc, decision, now=dt("2026-11-26T09:35:00"), post=post) == "model_invalid"
    fallback.assert_called_once_with(db, doc, doc["messages"][0], attempt=1)
    fallback.reset_mock()
    draft.side_effect = None
    draft.return_value = (None, {})
    assert worker.dispatch(db, job, doc, decision, now=dt("2026-11-26T09:35:00"), post=post) == "model_hold"
    fallback.assert_not_called()
    post.assert_not_called()
