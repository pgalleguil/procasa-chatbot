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
