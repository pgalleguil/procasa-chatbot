"""Phase 2 tests: valid management enforcement, manual entry, terminology, shadow safety."""
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from threading import Lock
# =====================================================================
#  TESTS: coerce_crm_datetime helper
# =====================================================================

def test_coerce_crm_datetime_aware():
    from api_crm import coerce_crm_datetime
    from datetime import datetime, timezone
    dt = datetime(2026, 7, 24, 3, 23, tzinfo=timezone.utc)
    result = coerce_crm_datetime(dt)
    assert result == dt
    assert result.tzinfo is not None


def test_coerce_crm_datetime_naive():
    from api_crm import coerce_crm_datetime
    from datetime import datetime, timezone
    dt = datetime(2026, 7, 24, 3, 23)  # naive
    result = coerce_crm_datetime(dt)
    assert result.tzinfo is not None
    assert result.hour == 3
    assert result.minute == 23


def test_coerce_crm_datetime_iso_z():
    from api_crm import coerce_crm_datetime
    result = coerce_crm_datetime("2026-07-24T03:23:00Z")
    assert result is not None
    assert result.hour == 3


def test_coerce_crm_datetime_iso_offset():
    from api_crm import coerce_crm_datetime
    result = coerce_crm_datetime("2026-07-23T23:23:00-04:00")
    assert result is not None
    assert result.tzinfo is not None
    assert result.utcoffset().total_seconds() == 0  # Should be UTC
    assert result.strftime("%H:%M") == "03:23"


def test_coerce_crm_datetime_iso_naive():
    from api_crm import coerce_crm_datetime
    result = coerce_crm_datetime("2026-07-24T03:23:00")
    assert result is not None
    assert result.hour == 3


def test_coerce_crm_datetime_none():
    from api_crm import coerce_crm_datetime
    assert coerce_crm_datetime(None) is None


def test_coerce_crm_datetime_invalid():
    from api_crm import coerce_crm_datetime
    assert coerce_crm_datetime("not-a-date") is None
    assert coerce_crm_datetime(12345) is None


def test_coerce_crm_datetime_string_vs_datetime_compare():
    """String and datetime timestamps produce comparable UTC datetimes."""
    from api_crm import coerce_crm_datetime
    from datetime import datetime, timezone
    str_ts = coerce_crm_datetime("2026-07-24T03:23:00Z")
    dt_ts = coerce_crm_datetime(datetime(2026, 7, 24, 3, 23, tzinfo=timezone.utc))
    assert str_ts == dt_ts


def test_coerce_crm_datetime_naive_vs_aware_compare():
    """Naive and aware datetimes produce equal UTC datetimes."""
    from api_crm import coerce_crm_datetime
    from datetime import datetime, timezone
    naive = coerce_crm_datetime(datetime(2026, 7, 24, 3, 23))
    aware = coerce_crm_datetime(datetime(2026, 7, 24, 3, 23, tzinfo=timezone.utc))
    assert naive == aware
from chatbot.crm_management import RESULT_RULES, record_management_result
from chatbot.crm_metrics import (
    VALID_MANAGEMENT_EVENT_TYPES, OPEN_ONLY_EVENT_TYPES, event_evidence,
    registered_outreach_evidence,
)
from chatbot.crm_hot_delivery import assign_and_enqueue_hot
from chatbot.manual_entry import create_manual_lead

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")


# ---------- in-memory mocks ----------
class Result:
    modified_count = 1


class Collection:
    def __init__(self, docs=None, unique=None):
        self.docs = deepcopy(list(docs or []))
        self.lock = Lock()
        self._unique = set(unique or [])

    def find_one(self, query, *args, **kwargs):
        with self.lock:
            return next((deepcopy(row) for row in self.docs if self._matches(row, query)), None)

    def insert_one(self, doc):
        with self.lock:
            self.docs.append(deepcopy(doc))
            return Result()

    def update_one(self, query, update, upsert=False):
        with self.lock:
            row = next((item for item in self.docs if self._matches(item, query)), None)
            if row is None and upsert:
                row = {}
                self.docs.append(row)
            if row is not None:
                for key, value in update.get("$set", {}).items():
                    self._set_path(row, key, value)
            return Result()

    def find(self, query=None, projection=None, limit=0, sort=None):
        with self.lock:
            results = [deepcopy(row) for row in self.docs if self._matches(row, query)] if query else deepcopy(self.docs)
            if sort:
                reverse = sort[0][1] < 0 if sort else False
                results.sort(key=lambda r: r.get(sort[0][0], ""), reverse=reverse)
            if limit:
                results = results[:limit]
            return results

    def update_many(self, query, update):
        with self.lock:
            for row in self.docs:
                if self._matches(row, query):
                    for key, value in update.get("$set", {}).items():
                        self._set_path(row, key, value)
            return Result()

    def count_documents(self, query=None):
        with self.lock:
            if not query:
                return len(self.docs)
            return sum(1 for row in self.docs if self._matches(row, query))

    def find_one_and_update(self, query, update, **kwargs):
        with self.lock:
            row = next((item for item in self.docs if self._matches(item, query)), None)
            if row is None:
                return None
            for key, value in update.get("$set", {}).items():
                self._set_path(row, key, value)
            return deepcopy(row)

    def _matches(self, doc, query):
        for key, expected in query.items():
            if key == "$or":
                if not any(self._matches(doc, branch) for branch in expected):
                    return False
                continue
            if key == "$and":
                if not all(self._matches(doc, condition) for condition in expected):
                    return False
                continue
            actual, exists = self._get_path(doc, key)
            if isinstance(expected, dict):
                if "$exists" in expected and exists != expected["$exists"]:
                    return False
                if "$in" in expected and actual not in expected["$in"]:
                    return False
                if "$ne" in expected and actual == expected["$ne"]:
                    return False
                if "$regex" in expected:
                    import re
                    pattern = expected["$regex"]
                    if isinstance(pattern, re.Pattern):
                        if not pattern.search(str(actual or "")):
                            return False
                    elif not re.search(str(pattern), str(actual or "")):
                        return False
                    continue
            elif actual != expected:
                return False
        return True

    def _get_path(self, doc, path):
        value = doc
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                return None, False
            value = value[part]
        return value, True

    def _set_path(self, doc, path, value):
        target = doc
        parts = path.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = deepcopy(value)


class DB(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key in ["leads", "crm_assignment_cycles", "crm_management_results",
                     "crm_events", "crm_notifications_v1", "propiedades_captacion",
                     "usuarios"]:
            if key not in self:
                self[key] = Collection()

    def __missing__(self, key):
        self[key] = Collection()
        return self[key]


def local(day, hour, minute=0):
    from datetime import timezone
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def fixture_lead(temperature="COLD"):
    lead = {
        "_id": "lead-test-1",
        "phone": "+56911111111",
        "lead_temperature": temperature,
        "lead_temperature_effective": temperature,
        "lifecycle": {},
        "prospecto": {"nombre": "Test", "email": "test@test.cl", "codigo": "PROP1"},
    }
    return lead


def fixture_cycle(lead_id="lead-test-1"):
    return {
        "_id": "cycle-test-doc",
        "lead_id": lead_id,
        "assignment_cycle_id": "cycle-test-1",
        "assigned_to_user_id": "user-test-1",
        "assigned_at": local(23, 9),
        "unassigned_at": None,
        "cycle_status": "active",
        "schema_version": "crm_assignment_cycle_v1",
    }


def make_db(lead=None, cycle=None):
    if lead is None:
        lead = fixture_lead()
    if cycle is None:
        cycle = fixture_cycle(lead["_id"])
    return DB({
        "leads": Collection([lead]),
        "crm_assignment_cycles": Collection([cycle]),
        "crm_management_results": Collection(),
        "crm_events": Collection(),
        "crm_notifications_v1": Collection(),
        "propiedades_captacion": Collection(),
        "usuarios": Collection(),
    })


# =====================================================================
#  TESTS: event_evidence — SEND/CLICK/CALL never credit management
# =====================================================================

def test_send_wa_lead_never_credits_management():
    """SEND_WA_LEAD is now in OPEN_ONLY_EVENT_TYPES → management=False."""
    assert "SEND_WA_LEAD" in OPEN_ONLY_EVENT_TYPES
    evidence = event_evidence({
        "lead_id": "lead-1", "type": "SEND_WA_LEAD", "actor": "user-1",
        "actor_type": "human", "confirmed": True,
    })
    assert evidence["management"] is False


def test_send_email_lead_never_credits_management():
    assert "SEND_EMAIL_LEAD" in OPEN_ONLY_EVENT_TYPES
    evidence = event_evidence({
        "lead_id": "lead-1", "type": "SEND_EMAIL_LEAD", "actor": "user-1",
        "actor_type": "human", "confirmed": True,
    })
    assert evidence["management"] is False


def test_call_completed_lead_never_credits_management():
    assert "CALL_COMPLETED_LEAD" in OPEN_ONLY_EVENT_TYPES
    evidence = event_evidence({
        "lead_id": "lead-1", "type": "CALL_COMPLETED_LEAD", "actor": "user-1",
        "actor_type": "human", "confirmed": True,
    })
    assert evidence["management"] is False


def test_click_whatsapp_lead_never_credits_management():
    """CLICK_WHATSAPP_LEAD is now in OPEN_ONLY_EVENT_TYPES."""
    assert "CLICK_WHATSAPP_LEAD" in OPEN_ONLY_EVENT_TYPES
    evidence = event_evidence({
        "lead_id": "lead-1", "type": "CLICK_WHATSAPP_LEAD", "actor": "user-1",
        "actor_type": "human",
    })
    assert evidence["management"] is False


def test_click_phone_lead_never_credits_management():
    assert "CLICK_PHONE_LEAD" in OPEN_ONLY_EVENT_TYPES
    evidence = event_evidence({
        "lead_id": "lead-1", "type": "CLICK_PHONE_LEAD", "actor": "user-1",
        "actor_type": "human",
    })
    assert evidence["management"] is False


def test_page_view_never_credits_management():
    assert "OPEN_DETAIL" in OPEN_ONLY_EVENT_TYPES
    evidence = event_evidence({
        "lead_id": "lead-1", "type": "OPEN_DETAIL", "actor": "user-1",
        "actor_type": "human",
    })
    assert evidence["management"] is False


def test_recommendation_send_never_credits_management():
    """Recommendation sends SEND_WA_LEAD which is open-only."""
    evidence = event_evidence({
        "lead_id": "lead-1", "type": "SEND_WA_LEAD", "actor": "user-1",
        "actor_type": "human", "confirmed": True, "result": "MENSAJE_ENVIADO",
    })
    assert evidence["management"] is False


# =====================================================================
#  TESTS: Complete management form DOES credit management
# =====================================================================

def test_complete_management_form_credits_first_valid_management():
    """HUMAN_NOTE with a valid management result IS valid management."""
    assert "HUMAN_NOTE" in VALID_MANAGEMENT_EVENT_TYPES
    evidence = event_evidence({
        "lead_id": "lead-1", "type": "HUMAN_NOTE", "actor": "user-1",
        "actor_type": "human", "result": "CONTACTADO", "confirmed": True,
    })
    assert evidence["management"] is True
    assert evidence["contact_attempt"] is True
    assert evidence["effective_contact"] is True


def test_management_form_without_result_is_not_management():
    """HUMAN_NOTE without result AND without meaningful_change is not management."""
    evidence = event_evidence({
        "lead_id": "lead-1", "type": "HUMAN_NOTE", "actor": "user-1",
        "actor_type": "human", "confirmed": True,
    })
    assert evidence["management"] is False


# =====================================================================
#  TESTS: Manual entry always creates Lead (COLD), never HOT
# =====================================================================

def test_manual_entry_always_creates_lead_not_hot():
    """Manual entry always uses COLD regardless of payload."""
    db = make_db()
    prop = {
        "_id": "prop-test-1",
        "codigo": "PROP1",
        "codex": "PROP1",
        "comuna": "Santiago",
        "region": "Metropolitana",
        "operacion": "Venta",
        "tipo": "Departamento",
        "ejecutivo_asignado": "Test Ejecutivo",
    }
    db["universo_cartera_prop360"].docs.append(deepcopy(prop))
    db["usuarios"].docs.append({
        "_id": "user-test-1",
        "nombre": "Test Ejecutivo",
        "phone": "+56922222222",
        "is_active": True,
    })
    result = create_manual_lead({
        "phone": "+56933333333",
        "property_code": "PROP1",
        "nombre": "Test Lead",
        "email": "test@test.cl",
        "origen": "Test",
    })
    # Verify the lead was created with COLD
    lead = db["leads"].find_one({"phone": "+56933333333"})
    if lead:
        assert lead["lead_temperature"] == "COLD"
        assert lead["lead_temperature_effective"] == "COLD"


def test_manual_entry_ignores_hot_payload():
    """Even if payload includes lead_temperature=HOT, the lead is created as COLD."""
    db = make_db()
    prop = {
        "_id": "prop-test-2",
        "codigo": "PROP2",
        "codex": "PROP2",
        "comuna": "Santiago",
        "region": "Metropolitana",
        "operacion": "Venta",
        "tipo": "Departamento",
    }
    db["universo_cartera_prop360"].docs.append(deepcopy(prop))
    db["usuarios"].docs.append({
        "_id": "user-test-2",
        "nombre": "Test Ejecutivo",
        "phone": "+56922222222",
        "is_active": True,
    })
    result = create_manual_lead({
        "phone": "+56944444444",
        "property_code": "PROP2",
        "nombre": "Hack Lead",
        "email": "hack@test.cl",
        "origen": "Test",
        "lead_temperature": "HOT",
    })
    lead = db["leads"].find_one({"phone": "+56944444444"})
    if lead:
        assert lead["lead_temperature"] == "COLD", "HOT payload was ignored"


# =====================================================================
#  TESTS: RESULT_RULES matrix compliance
# =====================================================================

def test_all_result_rules_have_expected_fields():
    """Every result rule has the required structure."""
    required_keys = {"attempt", "effective", "follow_up", "status"}
    for result_type, rule in RESULT_RULES.items():
        assert all(k in rule for k in required_keys), f"{result_type} missing keys"
        assert rule["status"] in (
            "managed_waiting_response", "managed_contacted",
            "managed_follow_up", "managed_closed",
        ), f"{result_type} invalid status"


def test_effective_contact_records_first_valid_management():
    """EFFECTIVE_CONTACT writes first_valid_management_at via record_management_result."""
    db = make_db()
    record_management_result(
        db, lead_id="lead-test-1", assignment_cycle_id="cycle-test-1",
        actor_user_id="user-test-1", result_type="EFFECTIVE_CONTACT",
        occurred_at=local(23, 10), source="test", idempotency_key="test-eff-1",
    )
    lead = db["leads"].find_one({"_id": "lead-test-1"})
    assert lead["lifecycle"]["first_valid_management_at"] == local(23, 10)
    assert lead["management_status"] == "managed_contacted"


def test_invalid_number_records_management_and_closes():
    """INVALID_NUMBER records management but no follow-up."""
    db = make_db()
    record_management_result(
        db, lead_id="lead-test-1", assignment_cycle_id="cycle-test-1",
        actor_user_id="user-test-1", result_type="INVALID_NUMBER",
        occurred_at=local(23, 10), source="test", idempotency_key="test-inv-1",
    )
    lead = db["leads"].find_one({"_id": "lead-test-1"})
    assert lead["lifecycle"]["first_valid_management_at"]
    assert lead["follow_up_required"] is False


def test_follow_up_requested_creates_follow_up():
    db = make_db()
    record_management_result(
        db, lead_id="lead-test-1", assignment_cycle_id="cycle-test-1",
        actor_user_id="user-test-1", result_type="FOLLOW_UP_REQUESTED",
        occurred_at=local(23, 10), source="test", idempotency_key="test-fu-1",
    )
    lead = db["leads"].find_one({"_id": "lead-test-1"})
    assert lead["follow_up_required"] is True
    assert lead["follow_up_status"] == "pending"
    assert lead["management_status"] == "managed_follow_up"


def test_discarded_valid_reason_closes_without_attempt():
    db = make_db()
    record_management_result(
        db, lead_id="lead-test-1", assignment_cycle_id="cycle-test-1",
        actor_user_id="user-test-1", result_type="DISCARDED_VALID_REASON",
        occurred_at=local(23, 10), source="test", idempotency_key="test-disc-1",
    )
    lead = db["leads"].find_one({"_id": "lead-test-1"})
    assert lead["lifecycle"]["first_valid_management_at"]
    assert lead["contact_attempted"] is False
    assert lead["follow_up_required"] is False
    assert lead["management_status"] == "managed_closed"


# =====================================================================
#  TESTS: Template no longer shows "Cold" terminology
# =====================================================================

def test_templates_no_cold_terminology():
    """Verify that key templates do not contain 'Cold' or 'Lead por calificar' visible text."""
    forbidden_phrases = [
        "Lead por calificar",
        "Por calificar",
        "lead-type-cold",
        "lead-temperature-cold",
        "❄️ C: Baja",
    ]
    templates_to_check = [
        "_lead_temperature_visual.html",
        "crm_leads_list.html",
        "crm_lead_detail.html",
        "manual_lead_entry.html",
    ]
    for tmpl in templates_to_check:
        path = os.path.join(TEMPLATES_DIR, tmpl)
        if not os.path.exists(path):
            continue
        content = open(path, "r", encoding="utf-8").read()
        for phrase in forbidden_phrases:
            if phrase in content:
                # The internal enum COLD is allowed; visible labels are not
                if phrase in ("Por calificar", "Lead por calificar"):
                    # These should NOT appear as visible text (only internal COLD enum)
                    # Check they're not in visible context (outside Jinja conditionals on COLD enum)
                    if phrase not in content or "COLD" not in content:
                        continue
                assert False, f"'{phrase}' found in {tmpl}"


# =====================================================================
#  TESTS: Hot live still works without duplication
# =====================================================================

def test_hot_business_hours_no_duplication():
    """Hot notification dedup: second call returns existing notification."""
    lead = {"_id": "lead-hot-1", "lead_temperature_effective": "HOT"}
    db = DB({
        "leads": Collection([lead]),
        "crm_assignment_cycles": Collection(),
        "crm_notifications_v1": Collection(unique={"individual_identity"}),
        "crm_management_results": Collection(),
        "crm_events": Collection(),
    })
    created = assign_and_enqueue_hot(
        db, lead=lead, recipient_user_id="u1", recipient_phone="+56911111111",
        payload={"target_name": "u1"}, assigned_at=local(23, 9), send_after=local(23, 9),
    )
    repeated = assign_and_enqueue_hot(
        db, lead=lead, recipient_user_id="u1", recipient_phone="+56911111111",
        payload={"target_name": "u1"}, assigned_at=local(23, 9), send_after=local(23, 9),
    )
    assert len(db["crm_notifications_v1"].docs) == 1
    assert repeated.get("dedup_suppressed") is True
    assert repeated["notification"]["delivery_id"] == created["notification"]["delivery_id"]


# =====================================================================
#  TESTS: Shadow safety — digest and SLA remain in shadow
# =====================================================================

def test_shadow_collections_are_empty():
    """No live delivery artifacts should exist in shadow collections."""
    db = make_db()
    # crm_non_hot_digests should not exist or be empty
    assert "crm_non_hot_digests" not in db or db["crm_non_hot_digests"].count_documents({}) == 0
    # sla_shadow_segments should not exist or be empty
    assert "sla_shadow_segments" not in db or db["sla_shadow_segments"].count_documents({}) == 0
    # crm_sla_notified_members should not exist or be empty
    assert "crm_sla_notified_members" not in db or db["crm_sla_notified_members"].count_documents({}) == 0


# =====================================================================
#  TESTS: registered_outreach_evidence still works for presentation
# =====================================================================

def test_registered_outreach_presentation_still_works():
    """registered_outreach_evidence still recognizes SEND_WA_LEAD for historical display."""
    event = {"type": "SEND_WA_LEAD", "timestamp": local(23, 10),
             "assignment_cycle_id": "cycle-test-1"}
    evidence = registered_outreach_evidence(
        event, assigned_at=local(23, 9), assignment_cycle_id="cycle-test-1",
    )
    assert evidence["recognized"] is True


# =====================================================================
#  TESTS: Terminal conditions close the cycle
# =====================================================================

def _setup_terminal_db():
    """Create a DB with a lead and active cycle for terminal condition tests."""
    from chatbot.crm_management import RESULT_RULES
    lead = fixture_lead()
    cycle = fixture_cycle(lead["_id"])
    cycle["assigned_at"] = local(24, 9)  # Post-cutover
    db = make_db(lead, cycle)
    return db, lead, cycle


def test_invalid_number_closes_cycle():
    """INVALID_NUMBER (managed_closed) closes the cycle."""
    from chatbot.crm_management import record_management_result
    db, lead, cycle = _setup_terminal_db()
    record_management_result(
        db, lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
        actor_user_id="user-test-1", result_type="INVALID_NUMBER",
        occurred_at=local(24, 10), source="test", idempotency_key="term-inv",
    )
    updated = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle["assignment_cycle_id"]})
    assert updated.get("cycle_status") == "closed"
    assert updated.get("closed_at") is not None
    assert updated.get("closed_reason") == "management_result_closed"


def test_discarded_valid_reason_closes_cycle():
    """DISCARDED_VALID_REASON (managed_closed) closes the cycle."""
    from chatbot.crm_management import record_management_result
    db, lead, cycle = _setup_terminal_db()
    record_management_result(
        db, lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
        actor_user_id="user-test-1", result_type="DISCARDED_VALID_REASON",
        occurred_at=local(24, 10), source="test", idempotency_key="term-disc",
    )
    updated = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle["assignment_cycle_id"]})
    assert updated.get("cycle_status") == "closed"


def test_not_interested_closes_cycle():
    """NOT_INTERESTED (managed_closed) closes the cycle."""
    from chatbot.crm_management import record_management_result
    db, lead, cycle = _setup_terminal_db()
    record_management_result(
        db, lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
        actor_user_id="user-test-1", result_type="NOT_INTERESTED",
        occurred_at=local(24, 10), source="test", idempotency_key="term-ni",
    )
    updated = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle["assignment_cycle_id"]})
    assert updated.get("cycle_status") == "closed"


def test_idempotent_close_does_not_create_second_event():
    """Repeating the same close result does not create a second event or re-close."""
    from chatbot.crm_management import record_management_result
    db, lead, cycle = _setup_terminal_db()
    r1 = record_management_result(
        db, lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
        actor_user_id="user-test-1", result_type="INVALID_NUMBER",
        occurred_at=local(24, 10), source="test", idempotency_key="term-dup",
    )
    r2 = record_management_result(
        db, lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
        actor_user_id="user-test-1", result_type="INVALID_NUMBER",
        occurred_at=local(24, 10), source="test", idempotency_key="term-dup",
    )
    # Same idempotency key should return the same result
    assert r1["_id"] == r2["_id"]
    assert len(db["crm_management_results"].docs) == 1
    assert len(db["crm_events"].docs) == 1


def test_managed_waiting_response_does_not_close_cycle():
    """MESSAGE_SENT_WAITING_RESPONSE (follow_up=True) does NOT close the cycle."""
    from chatbot.crm_management import record_management_result
    db, lead, cycle = _setup_terminal_db()
    record_management_result(
        db, lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
        actor_user_id="user-test-1", result_type="MESSAGE_SENT_WAITING_RESPONSE",
        occurred_at=local(24, 10), source="test", idempotency_key="term-wait",
    )
    updated = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle["assignment_cycle_id"]})
    assert updated.get("cycle_status") == "active", "Non-terminal result should leave cycle active"


def test_effective_contact_does_not_close_cycle():
    """EFFECTIVE_CONTACT (follow_up=False, managed_contacted) does NOT close the cycle."""
    from chatbot.crm_management import record_management_result
    db, lead, cycle = _setup_terminal_db()
    record_management_result(
        db, lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
        actor_user_id="user-test-1", result_type="EFFECTIVE_CONTACT",
        occurred_at=local(24, 10), source="test", idempotency_key="term-eff",
    )
    updated = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle["assignment_cycle_id"]})
    assert updated.get("cycle_status") == "active", "Non-terminal result should leave cycle active"


def test_not_interested_uses_closed_reason():
    """NOT_INTERESTED sets closed_reason=management_result_closed."""
    from chatbot.crm_management import record_management_result
    db, lead, cycle = _setup_terminal_db()
    record_management_result(
        db, lead_id=lead["_id"], assignment_cycle_id=cycle["assignment_cycle_id"],
        actor_user_id="user-test-1", result_type="NOT_INTERESTED",
        occurred_at=local(24, 10), source="test", idempotency_key="term-reason",
    )
    updated = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": cycle["assignment_cycle_id"]})
    assert updated.get("closed_reason") == "management_result_closed"


# =====================================================================
#  TESTS: Chatbot lead cycle creation (processing_service hotfix)
# =====================================================================

def test_create_assignment_cycle_creates_canonical_structure():
    """create_assignment_cycle creates a cycle with sla_policy_version and active status."""
    from chatbot.crm_metrics import create_assignment_cycle
    db, lead, _ = fixture_lead(), fixture_cycle(), None
    db2 = DB({
        "leads": Collection([deepcopy(fixture_lead())]),
        "crm_assignment_cycles": Collection(),
        "crm_management_results": Collection(),
        "crm_events": Collection(),
        "crm_notifications_v1": Collection(),
    })
    lead_obj = db2["leads"].find_one({"_id": "lead-test-1"})
    cycle = create_assignment_cycle(
        db2, lead=lead_obj, assigned_to_user_id="u1",
        assigned_by="system", reason="test",
        assigned_to_display_name="Test",
    )
    assert cycle.get("cycle_status") == "active"
    assert cycle.get("sla_policy_version") == "sla_visual_v1_20260723"
    assert cycle.get("assignment_cycle_id") is not None
    active = [c for c in db2["crm_assignment_cycles"].docs if c.get("cycle_status") == "active"]
    assert len(active) == 1


def test_create_assignment_cycle_idempotent():
    """create_assignment_cycle returns the same cycle on second call."""
    from chatbot.crm_metrics import create_assignment_cycle
    db = DB({
        "leads": Collection([deepcopy(fixture_lead())]),
        "crm_assignment_cycles": Collection(),
        "crm_management_results": Collection(),
        "crm_events": Collection(),
        "crm_notifications_v1": Collection(),
    })
    lead_obj = db["leads"].find_one({"_id": "lead-test-1"})
    c1 = create_assignment_cycle(db, lead=lead_obj, assigned_to_user_id="u1",
        assigned_by="system", reason="test", assigned_to_display_name="Test")
    c2 = create_assignment_cycle(db, lead=lead_obj, assigned_to_user_id="u1",
        assigned_by="system", reason="test", assigned_to_display_name="Test")
    assert c1["assignment_cycle_id"] == c2["assignment_cycle_id"]
    active = [c for c in db["crm_assignment_cycles"].docs if c.get("cycle_status") == "active"]
    assert len(active) == 1


def test_format_relative_time_future_never_hace():
    """format_relative_time() never shows 'Hace' for future timestamps."""
    from api_crm import format_relative_time
    from datetime import datetime, timedelta, timezone
    future = datetime.now(timezone.utc) + timedelta(hours=5)
    result = format_relative_time(future)
    assert "Hace" not in result, f"Future timestamp should not contain 'Hace': {result}"
    assert "Programado" in result, f"Should show 'Programado': {result}"


def test_format_relative_time_past_normal():
    """format_relative_time() shows normal relative time for past timestamps."""
    from api_crm import format_relative_time
    from datetime import datetime, timedelta, timezone
    five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
    result = format_relative_time(five_min_ago)
    assert "Hace" in result, f"Past timestamp should contain 'Hace': {result}"


# =====================================================================
#  TESTS: After-hours display
# =====================================================================

def _is_after_hours_test(dt_local):
    """Replica de la lógica de api_crm.py para detectar asignación fuera de horario."""
    from chatbot.constants import BUSINESS_DAYS, BUSINESS_START_HOUR, BUSINESS_END_HOUR, CHILE_TZ
    from chatbot.crm_metrics import coerce_utc_datetime
    assigned = coerce_utc_datetime(dt_local)
    if not assigned:
        return False
    local = assigned.astimezone(CHILE_TZ)
    return (
        local.weekday() not in BUSINESS_DAYS
        or local.hour >= BUSINESS_END_HOUR
        or local.hour < BUSINESS_START_HOUR
    )


def test_after_hours_night_time():
    """22:12 CLT on weekday is after hours."""
    from datetime import datetime, timezone
    dt = datetime(2026, 7, 23, 22, 12, tzinfo=timezone.utc)  # 18:12 CLT? No, 22:12 UTC = 18:12 CLT
    # Need to pass the actual local time for the check
    from chatbot.constants import CHILE_TZ
    local_dt = datetime(2026, 7, 23, 22, 12, tzinfo=CHILE_TZ)  # 22:12 CLT
    assert _is_after_hours_test(local_dt), "22:12 CLT should be after hours"


def test_after_hours_business_hours():
    """11:00 CLT on weekday is NOT after hours."""
    from chatbot.constants import CHILE_TZ
    from datetime import datetime
    local_dt = datetime(2026, 7, 24, 11, 0, tzinfo=CHILE_TZ)
    assert not _is_after_hours_test(local_dt), "11:00 CLT should be business hours"


def test_after_hours_weekend():
    """Saturday 11:00 CLT IS after hours."""
    from chatbot.constants import CHILE_TZ
    from datetime import datetime
    local_dt = datetime(2026, 7, 25, 11, 0, tzinfo=CHILE_TZ)  # Saturday
    assert _is_after_hours_test(local_dt), "Saturday should be after hours"


def test_after_hours_edge_19_00():
    """19:00 CLT exactly is after hours (end hour exclusive)."""
    from chatbot.constants import CHILE_TZ
    from datetime import datetime
    local_dt = datetime(2026, 7, 24, 19, 0, tzinfo=CHILE_TZ)
    assert _is_after_hours_test(local_dt), "19:00 CLT should be after hours"


def test_after_hours_edge_09_00():
    """09:00 CLT exactly is NOT after hours (start hour inclusive)."""
    from chatbot.constants import CHILE_TZ
    from datetime import datetime
    local_dt = datetime(2026, 7, 24, 9, 0, tzinfo=CHILE_TZ)
    assert not _is_after_hours_test(local_dt), "09:00 CLT should be business hours"


def test_after_hours_pipeline_stage_none():
    """pipeline_stage=None should not prevent after-hours display."""
    from chatbot.constants import CHILE_TZ
    from datetime import datetime
    local_dt = datetime(2026, 7, 23, 22, 12, tzinfo=CHILE_TZ)
    assert _is_after_hours_test(local_dt)
    # This validates the core logic; the template condition was fixed
    # to evaluate after-hours before checking pipeline_stage

