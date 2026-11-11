"""Pruebas sin red, MongoDB ni cuentas de WaSender."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from unittest import TestCase

from chatbot.executive_whatsapp_audit import (
    ExecutiveAuditError, audit_transcript, ingest_upsert,
    normalize_upsert, verify_webhook_secret,
)

SESSION = "executive-session-1"
EXECUTIVE = "user-hernan"
PHONE = "+56912345678"
STAMP = 1794394800  # UNIX timestamp de prueba; siempre posterior al ciclo.


class FakeResult:
    def __init__(self, count):
        self.modified_count = count


class FakeCollection:
    def __init__(self, name, db):
        self.name = name
        self.db = db

    def find(self, selector, projection=None):
        if self.name == "leads":
            variants = set()
            for row in selector.get("$or", []):
                for value in row.values():
                    variants.update(value.get("$in", []))
            return [deepcopy(doc) for doc in self.db.leads
                    if doc.get("phone") in variants or
                    doc.get("contact_phone_normalized") in variants]
        if self.name == "crm_assignment_cycles":
            ids = set(selector["lead_id"]["$in"])
            return [deepcopy(row) for row in self.db.cycles if row["lead_id"] in ids]
        raise AssertionError("unexpected_find")

    def find_one(self, selector, projection=None):
        if self.name != "leads":
            raise AssertionError("unexpected_find_one")
        expected_id = selector.get("_id")
        provider_key = selector.get("executive_whatsapp_messages.provider_key")
        for doc in self.db.leads:
            if doc["_id"] == expected_id and any(
                msg.get("provider_key") == provider_key
                for msg in doc.get("executive_whatsapp_messages", [])
            ):
                return {"_id": expected_id}
        return None

    def update_one(self, selector, update, upsert=False):
        assert self.name == "leads"
        assert upsert is False
        assert set(update) == {"$push"}
        for doc in self.db.leads:
            if doc["_id"] != selector["_id"]:
                continue
            provider_key = selector["executive_whatsapp_messages.provider_key"]["$ne"]
            existing = doc.setdefault("executive_whatsapp_messages", [])
            if any(x["provider_key"] == provider_key for x in existing):
                return FakeResult(0)
            existing.append(deepcopy(update["$push"]["executive_whatsapp_messages"]))
            return FakeResult(1)
        return FakeResult(0)


class FakeDB:
    def __init__(self, *, owner=EXECUTIVE, cycle_start="2026-11-01T09:00:00+00:00"):
        self.leads = [
            {"_id": "L1", "phone": PHONE, "messages": [
                {"role": "user", "content": "Hola", "timestamp": "2026-11-01T09:01:00Z"},
                {"role": "assistant", "content": "Bienvenido", "timestamp": "2026-11-01T09:02:00Z"},
            ]},
        ]
        self.cycles = [
            {"lead_id": "L1", "assignment_cycle_id": "C1",
             "assigned_to_user_id": owner, "assigned_at": cycle_start,
             "cycle_status": "active"},
        ]

    def __getitem__(self, key):
        return FakeCollection(key, self)


def event(*, outgoing=False, jid="56912345678@s.whatsapp.net",
          ident="ID1", body="Todavía busco departamento"):
    return {
        "event": "messages.upsert", "timestamp": STAMP,
        "data": {"messages": {
            "key": {"remoteJid": jid, "id": ident, "fromMe": outgoing},
            "messageBody": body,
            "messageTimestamp": STAMP,
            "message": {"conversation": body},
        }},
    }


def ingest(db, payload, commit=False, signature="secret"):
    return ingest_upsert(
        db, payload, session_id=SESSION, executive_user_id=EXECUTIVE,
        signature_header=signature, webhook_secret="secret", commit=commit,
    )


class TestExecutiveWhatsAppAudit(TestCase):
    def test_signature_required_and_checked_constant_time(self):
        for supplied in (None, "", "wrong"):
            with self.assertRaises(ExecutiveAuditError):
                verify_webhook_secret(supplied, "secret")
        verify_webhook_secret("secret", "secret")

    def test_dry_run_does_not_write_or_change_chatbot(self):
        db = FakeDB()
        before = deepcopy(db.leads)
        result = ingest(db, event())
        self.assertEqual(result["results"], {"planned": 1})
        self.assertEqual(db.leads, before)
        self.assertEqual(result["outbound_messages_sent"], 0)

    def test_persist_and_idempotence_without_touching_chatbot_messages(self):
        db = FakeDB()
        old_messages = deepcopy(db.leads[0]["messages"])
        first = ingest(db, event(), commit=True)
        repeated = ingest(db, event(), commit=True)
        self.assertEqual(first["results"], {"saved": 1})
        self.assertEqual(repeated["results"], {"duplicate_confirmed": 1})
        self.assertEqual(len(db.leads[0]["executive_whatsapp_messages"]), 1)
        saved = db.leads[0]["executive_whatsapp_messages"][0]
        self.assertEqual(saved["assignment_cycle_id"], "C1")
        self.assertEqual(saved["actor_type"], "client")
        self.assertEqual(db.leads[0]["messages"], old_messages)

    def test_outbound_attributed_to_executive(self):
        db = FakeDB()
        result = ingest(db, event(outgoing=True), commit=True)
        self.assertEqual(result["results"], {"saved": 1})
        self.assertEqual(
            db.leads[0]["executive_whatsapp_messages"][0]["actor_type"], "executive"
        )

    def test_reassignment_blocks_old_owner(self):
        db = FakeDB()
        db.cycles[0]["unassigned_at"] = "2026-11-02T00:00:00Z"
        db.cycles.append({
            "lead_id": "L1", "assignment_cycle_id": "C2",
            "assigned_to_user_id": "user-mariela",
            "assigned_at": "2026-11-02T00:00:00Z", "cycle_status": "active",
        })
        result = ingest(db, event(), commit=True)
        self.assertEqual(result["results"], {"no_confirmed_assignment_at_message_time": 1})
        self.assertNotIn("executive_whatsapp_messages", db.leads[0])

    def test_reject_non_lead_personal_chat_and_group(self):
        db = FakeDB()
        unknown = ingest(db, event(jid="56999998888@s.whatsapp.net"), commit=True)
        group = ingest(db, event(jid="1200000@g.us"), commit=True)
        self.assertEqual(unknown["results"], {"lead_not_found": 1})
        self.assertEqual(group["results"], {"unresolved_or_nonprivate_peer": 1})
        self.assertNotIn("executive_whatsapp_messages", db.leads[0])

    def test_lid_without_proven_client_phone_rejected(self):
        records, skipped = normalize_upsert(
            event(jid="123456789@lid", outgoing=True),
            session_id=SESSION, executive_user_id=EXECUTIVE,
        )
        self.assertEqual(records, [])
        self.assertEqual(skipped, ["unresolved_or_nonprivate_peer"])

    def test_incoming_lid_may_use_cleaned_sender_pn(self):
        payload = event(jid="123456789@lid")
        payload["data"]["messages"]["key"]["cleanedSenderPn"] = "56912345678"
        records, skipped = normalize_upsert(
            payload, session_id=SESSION, executive_user_id=EXECUTIVE,
        )
        self.assertEqual(skipped, [])
        self.assertEqual(records[0]["phone"], PHONE)

    def test_single_and_array_payload_and_batch_dedupe(self):
        db = FakeDB()
        a = event()["data"]["messages"]
        duplicate = deepcopy(a)
        result = ingest(db, {
            "event": "messages.upsert", "timestamp": STAMP,
            "data": {"messages": [a, duplicate]},
        }, commit=True)
        self.assertEqual(result["results"], {"saved": 1, "duplicate_in_batch": 1})

    def test_timestamp_and_direction_are_required(self):
        payload = event()
        del payload["data"]["messages"]["key"]["fromMe"]
        records, skipped = normalize_upsert(
            payload, session_id=SESSION, executive_user_id=EXECUTIVE,
        )
        self.assertEqual(records, [])
        self.assertEqual(skipped, ["missing_message_direction"])

    def test_transcript_unifies_but_keeps_sources_separate(self):
        db = FakeDB()
        ingest(db, event(), commit=True)
        rows = audit_transcript(db.leads[0])
        self.assertEqual({x["actor_type"] for x in rows},
                         {"client", "chatbot"})
        self.assertEqual(rows[-1]["source"], "executive_whatsapp")
        self.assertTrue(rows[-1]["evidence_key"])

    def test_ambiguous_multiple_leads_not_written(self):
        db = FakeDB()
        db.leads.append({"_id": "L2", "phone": PHONE, "messages": []})
        db.cycles.append({
            "lead_id": "L2", "assignment_cycle_id": "C3",
            "assigned_to_user_id": EXECUTIVE, "assigned_at": "2026-11-01T09:00:00Z",
            "cycle_status": "active",
        })
        result = ingest(db, event(), commit=True)
        self.assertEqual(result["results"], {"ambiguous_phone_lead_identity": 1})
        self.assertTrue(all("executive_whatsapp_messages" not in x for x in db.leads))

    def test_duplicate_phone_is_always_ambiguous_even_when_one_lead_unassigned(self):
        db = FakeDB()
        db.leads.append({"_id": "L2", "phone": PHONE, "messages": []})
        result = ingest(db, event(), commit=True)
        self.assertEqual(result["results"], {"ambiguous_phone_lead_identity": 1})
        self.assertTrue(all("executive_whatsapp_messages" not in x for x in db.leads))

    def test_evidence_key_is_stable_but_does_not_include_phone(self):
        first, _ = normalize_upsert(
            event(), session_id=SESSION, executive_user_id=EXECUTIVE,
        )
        second, _ = normalize_upsert(
            event(), session_id=SESSION, executive_user_id=EXECUTIVE,
        )
        self.assertEqual(first[0]["provider_key"], second[0]["provider_key"])
        self.assertNotIn("56912345678", first[0]["provider_key"])
        self.assertEqual(len(first[0]["provider_key"].removeprefix("wasender:")), 64)

    def test_naive_timestamp_rejected_instead_of_guessed_utc(self):
        payload = event()
        del payload["data"]["messages"]["messageTimestamp"]
        payload["data"]["messages"]["timestamp"] = "2026-11-11T09:00:00"
        del payload["timestamp"]
        records, skipped = normalize_upsert(
            payload, session_id=SESSION, executive_user_id=EXECUTIVE,
        )
        self.assertEqual(records, [])
        self.assertEqual(skipped, ["missing_timestamp"])

    def test_live_atomic_bson_capacity_condition_always_present(self):
        db = FakeDB()
        result = ingest(db, event(), commit=True)
        self.assertEqual(result["results"], {"saved": 1})
        # This fake DB does not evaluate expressions, but the production query
        # is instrumented below in an exact spy to inspect its safety contract.
        class SpyDB(FakeDB):
            def __getitem__(self, key):
                if key == "leads":
                    parent = self
                    class SpyCollection(FakeCollection):
                        def update_one(self, selector, update, upsert=False):
                            parent.last_write_filter = selector
                            return super().update_one(selector, update, upsert)
                    return SpyCollection(key, self)
                return super().__getitem__(key)
        spy = SpyDB()
        ingest(spy, event(), commit=True)
        self.assertIn("$expr", spy.last_write_filter)
        self.assertIn("$bsonSize", str(spy.last_write_filter["$expr"]))
        self.assertIn("executive_whatsapp_messages.provider_key", spy.last_write_filter)

    def test_rejects_history_backfill_without_original_message_timestamp(self):
        db = FakeDB()
        payload = event()
        del payload["data"]["messages"]["messageTimestamp"]
        result = ingest(db, payload, commit=True)
        self.assertEqual(result["results"], {"unverified_original_timestamp": 1})
        self.assertNotIn("executive_whatsapp_messages", db.leads[0])

    def test_zero_modified_is_not_falsely_called_a_duplicate(self):
        class NoWriteCollection(FakeCollection):
            def update_one(self, selector, update, upsert=False):
                return FakeResult(0)
        class NoWriteDB(FakeDB):
            def __getitem__(self, key):
                if key == "leads":
                    return NoWriteCollection(key, self)
                return super().__getitem__(key)
        db = NoWriteDB()
        result = ingest(db, event(), commit=True)
        self.assertEqual(result["results"], {"retryable_write_unconfirmed": 1})
        self.assertNotIn("executive_whatsapp_messages", db.leads[0])

    def test_saved_message_has_original_and_capture_timestamps(self):
        db = FakeDB()
        ingest(db, event(), commit=True)
        saved = db.leads[0]["executive_whatsapp_messages"][0]
        self.assertIsNotNone(saved.get("captured_at"))
        self.assertEqual(saved["timestamp_origin"], "message_timestamp")

    def test_fresh_envelope_only_message_is_stored_but_time_unverified(self):
        db = FakeDB()
        payload = event(outgoing=True)
        del payload["data"]["messages"]["messageTimestamp"]
        received_at = datetime.fromtimestamp(STAMP + 8, tz=timezone.utc)
        result = ingest_upsert(
            db, payload, session_id=SESSION, executive_user_id=EXECUTIVE,
            signature_header="secret", webhook_secret="secret", commit=True,
            received_at_utc=received_at,
        )
        self.assertEqual(result["results"], {"saved": 1})
        saved = db.leads[0]["executive_whatsapp_messages"][0]
        self.assertEqual(saved["timestamp_origin"], "webhook_timestamp")
        self.assertFalse(saved["timestamp_verified"])
        self.assertFalse(saved["time_usable_for_sla"])
        self.assertEqual(saved["cycle_attribution"], "current_assignment_at_receipt")

    def test_rejects_stale_envelope_even_when_phone_matches(self):
        db = FakeDB()
        payload = event()
        del payload["data"]["messages"]["messageTimestamp"]
        later = datetime.fromtimestamp(STAMP + 901, tz=timezone.utc)
        result = ingest_upsert(
            db, payload, session_id=SESSION, executive_user_id=EXECUTIVE,
            signature_header="secret", webhook_secret="secret", commit=True,
            received_at_utc=later,
        )
        self.assertEqual(result["results"], {"unverified_original_timestamp": 1})
        self.assertNotIn("executive_whatsapp_messages", db.leads[0])
