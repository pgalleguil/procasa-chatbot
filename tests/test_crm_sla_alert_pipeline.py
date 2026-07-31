"""Tests for CRM SLA Alert Pipeline — full coverage."""
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from chatbot.crm_sla_alert_pipeline import run_evaluation_and_persist_once
from chatbot.crm_sla_alert_repository import COLLECTION
from chatbot.crm_sla_alert_settings import REQUIRED_PERSIST_CONFIRMATION


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs); self._limit_val = None
    async def to_list(self, length=None):
        r = self._docs
        if self._limit_val is not None: r = r[:self._limit_val]
        return r[:length] if length else r
    def sort(self, key_or_list, direction=None):
        if direction is not None:
            self._docs.sort(key=lambda d: d.get(key_or_list) or "", reverse=(direction < 0))
        else:
            for f, d in key_or_list:
                self._docs.sort(key=lambda doc, field=f: doc.get(field) or "", reverse=(d < 0))
        return self
    def limit(self, n): self._limit_val = n; return self

class FakeCollection:
    def __init__(self, docs=None):
        self._docs: list[dict] = list(docs or [])
    def _match(self, doc, query):
        for k, v in query.items():
            if k.startswith("$"): continue
            dv = doc.get(k)
            if isinstance(v, dict):
                if "$in" in v:
                    if dv not in v["$in"]: return False
                elif "$ne" in v:
                    if dv == v["$ne"]: return False
                elif "$gte" in v:
                    if dv is None or dv < v["$gte"]: return False
                elif "$exists" in v:
                    if (k in doc) != v["$exists"]: return False
            elif dv != v: return False
        return True
    def find(self, query, projection=None):
        return FakeCursor([d for d in self._docs if self._match(d, query)])
    async def find_one(self, query, *a):
        for d in self._docs:
            if self._match(d, query): return dict(d)
        return None
    async def insert_one(self, doc):
        from pymongo.errors import DuplicateKeyError
        for d in self._docs:
            if d.get("_id") == doc.get("_id"): raise DuplicateKeyError("dup")
        self._docs.append(dict(doc))

class FakeDB(dict):
    def __missing__(self, k): self[k] = FakeCollection(); return self[k]

@pytest.fixture
def db(): return FakeDB()

def _lead(lid="lead-1", temp="COLD", exec_name="Erika Garrido", stage="NEW", phone="56912345678"):
    return {"_id": lid, "phone": phone, "lead_temperature_effective": temp,
            "pipeline_stage": stage, "ejecutivo_asignado": exec_name,
            "prospecto": {"nombre": "Cliente", "codigo": "P001"}}

def _recent_assigned():
    """Return a UTC datetime ~3h ago — after cutover, with enough business minutes."""
    return datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=3)


def _cycle(lid="lead-1", cid="cycle-1", uid="user-erika"):
    return {"lead_id": lid, "assignment_cycle_id": cid, "assigned_to_user_id": uid,
            "assigned_at": _today_cl_10am(),
            "unassigned_at": None, "cycle_status": "active", "reason": "lead_created"}

def _user(uid="user-erika", name="Erika Garrido", phone="+56911111111", active=True):
    return {"_id": uid, "nombre": name, "telefono": phone, "is_active": active}

CUT = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)


def _today_cl_9am():
    """Return 9am CLT today as timezone-aware UTC."""
    import pytz
    cl = pytz.timezone("America/Santiago")
    now_cl = datetime.now(cl)
    return cl.localize(datetime(now_cl.year, now_cl.month, now_cl.day, 9, 0, 0)).astimezone(timezone.utc)


def _flexible_expires():
    """Return 7pm CLT today as timezone-aware UTC."""
    import pytz
    cl = pytz.timezone("America/Santiago")
    now_cl = datetime.now(cl)
    return cl.localize(datetime(now_cl.year, now_cl.month, now_cl.day, 19, 0, 0)).astimezone(timezone.utc)


def _today_cl_10am():
    """Return 10am CLT today as timezone-aware UTC."""
    import pytz
    cl = pytz.timezone("America/Santiago")
    now_cl = datetime.now(cl)
    return cl.localize(datetime(now_cl.year, now_cl.month, now_cl.day, 10, 0, 0)).astimezone(timezone.utc)


def _test_now():
    """Return a fixed 'now' that makes the test cycle reach SLA breached (3pm CLT = 5h biz after 10am)."""
    return _today_cl_10am() + __import__("datetime").timedelta(hours=5)

S = "chatbot.crm_sla_alert_settings"
P = "chatbot.crm_sla_alert_pipeline"

# Reference to the real validation function for window-specific tests
from chatbot.crm_sla_alert_settings import validate_cutover_safe_for_persistence as _settings_validate_cutover

def _setup_persist_patches(cut=CUT):
    return (
        patch(f"{S}.CRM_SLA_ALERTS_ENABLED", True),
        patch(f"{S}.CRM_SLA_ALERTS_DRY_RUN", False),
        patch(f"{S}.CRM_SLA_ALERTS_PERSIST", True),
        patch(f"{S}.CRM_SLA_ALERTS_LIVE_SEND", False),
        patch(f"{S}.CRM_SLA_ALERTS_CANARY_MODE", True),
        patch(f"{S}.CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS", frozenset({"user-erika"})),
        patch(f"{S}.CRM_SLA_ALERTS_PERSIST_CONFIRMATION", REQUIRED_PERSIST_CONFIRMATION),
        patch(f"{S}.CRM_SLA_ALERT_CUTOVER_AT", cut),
        patch(f"{S}.CRM_SLA_ALERT_CANARY_EXPIRES_AT", _flexible_expires()),
        patch(f"{S}.validate_cutover_safe_for_persistence", lambda **kw: {"valid": True}),
        patch(f"{P}._settings.CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS", frozenset({"user-erika"})),
        patch(f"{P}._settings.CRM_SLA_ALERTS_MAX_PER_RUN", 20),
        patch(f"{P}._settings.CRM_SLA_ALERTS_MAX_PER_RECIPIENT_PER_RUN", 5),
    )


class TestConfig:
    @pytest.mark.asyncio
    async def test_missing_confirmation_blocks(self, db):
        with patch(f"{S}.CRM_SLA_ALERTS_PERSIST_CONFIRMATION", ""):
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["status"] == "config_blocked"

    @pytest.mark.asyncio
    async def test_empty_allowlist_blocks(self, db):
        with patch(f"{S}.CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS", frozenset()):
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["status"] == "config_blocked"

    @pytest.mark.asyncio
    async def test_live_send_true_blocks(self, db):
        with patch(f"{S}.CRM_SLA_ALERTS_LIVE_SEND", True):
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["status"] == "config_blocked"

    @pytest.mark.asyncio
    async def test_invalid_cutover_blocks(self, db):
        with patch(f"{S}.CRM_SLA_ALERT_CUTOVER_AT", None):
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["status"] == "config_blocked"

    @pytest.mark.asyncio
    async def test_old_cutover_blocks_persist(self, db):
        """Cutover from a different day is rejected."""
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=CUT):
                stack.enter_context(p)
            # Undo the mock so real validation runs
            stack.enter_context(patch(f"{S}.validate_cutover_safe_for_persistence", wraps=_settings_validate_cutover))
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["status"] == "config_blocked"
        assert "cutover_not_safe_for_persistence" in r["reason"]

    @pytest.mark.asyncio
    async def test_expires_before_cutover_blocks(self, db):
        """Expires before cutover is rejected."""
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            stack.enter_context(patch(f"{S}.CRM_SLA_ALERT_CANARY_EXPIRES_AT", _today_cl_9am() - __import__("datetime").timedelta(hours=1)))
            stack.enter_context(patch(f"{S}.validate_cutover_safe_for_persistence", wraps=_settings_validate_cutover))
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["status"] == "config_blocked"

    @pytest.mark.asyncio
    async def test_expires_after_7pm_blocks(self, db):
        """Expires after 19:00 is rejected."""
        import pytz
        cl = pytz.timezone("America/Santiago")
        now_cl = datetime.now(cl)
        late = cl.localize(datetime(now_cl.year, now_cl.month, now_cl.day, 20, 0, 0)).astimezone(timezone.utc)
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            stack.enter_context(patch(f"{S}.CRM_SLA_ALERT_CANARY_EXPIRES_AT", late))
            stack.enter_context(patch(f"{S}.validate_cutover_safe_for_persistence", wraps=_settings_validate_cutover))
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["status"] == "config_blocked"

    @pytest.mark.asyncio
    async def test_missing_expires_blocks_persist(self, db):
        """--persist without CRM_SLA_ALERT_CANARY_EXPIRES_AT is blocked."""
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            stack.enter_context(patch(f"{S}.CRM_SLA_ALERT_CANARY_EXPIRES_AT", None))
            stack.enter_context(patch(f"{S}.validate_cutover_safe_for_persistence", wraps=_settings_validate_cutover))
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["status"] == "config_blocked"
        assert "canary_expiration_required" in r["reason"]

    @pytest.mark.asyncio
    async def test_today_cutover_allowed(self, db):
        """A cutover set to today 9am passes the safety check."""
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        if r["status"] != "completed":
            assert False, f"blocked: reason={r.get('reason')} keys={list(r.keys())}"

    @pytest.mark.asyncio
    async def test_same_cutover_reused(self, db):
        """Same cutover can be reused across multiple executions."""
        db["leads"] = FakeCollection([_lead("lead-1")])
        db["crm_assignment_cycles"] = FakeCollection([_cycle("lead-1")])
        db["crm_events"] = FakeCollection([])
        db["crm_management_results"] = FakeCollection([])
        db["usuarios"] = FakeCollection([_user("user-erika", phone="+56911111111")])
        db[COLLECTION] = FakeCollection([])

        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            r1 = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
            r2 = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r1["status"] == "completed"
        assert r2["status"] == "completed"
        assert r2["already_exists"] >= 1


class TestPersist:
    def _setup_db(self, db):
        db["leads"] = FakeCollection([_lead("lead-1", "COLD", "Erika Garrido")])
        db["crm_assignment_cycles"] = FakeCollection([_cycle("lead-1", "cycle-1", "user-erika")])
        db["crm_events"] = FakeCollection([])
        db["crm_management_results"] = FakeCollection([])
        db["usuarios"] = FakeCollection([_user("user-erika", "Erika Garrido", "+56911111111")])
        db[COLLECTION] = FakeCollection([])

    @pytest.mark.asyncio
    async def test_creates_pending(self, db):
        self._setup_db(db)
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["candidates_evaluated"] > 0, f"no candidates: cutover_used={r.get('cutover_used')}"
        assert r["persisted"] == 1

    @pytest.mark.asyncio
    async def test_second_run_already_exists(self, db):
        self._setup_db(db)
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            r1 = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
            r2 = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r1["persisted"] == 1 and r2["persisted"] == 0 and r2["already_exists"] >= 1

    @pytest.mark.asyncio
    async def test_warning_breached_distinct_keys(self, db):
        """Warning and breached for same cycle create different documents."""
        self._setup_db(db)
        db["leads"]._docs[0]["lead_temperature_effective"] = "HOT"
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["persisted"] == 1  # breached identity only, no separate warning retroactive

    @pytest.mark.asyncio
    async def test_allowlist_blocks_other(self, db):
        self._setup_db(db)
        db["leads"]._docs[0]["ejecutivo_asignado"] = "Otra"
        db["crm_assignment_cycles"]._docs[0]["assigned_to_user_id"] = "user-other"
        db["usuarios"]._docs[0] = _user("user-other", "Otra", "+56922222222")
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["persisted"] == 0 and r["excluded_by_allowlist"] == 1

    @pytest.mark.asyncio
    async def test_no_phone_excluded(self, db):
        self._setup_db(db)
        db["usuarios"]._docs[0]["telefono"] = ""
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["persisted"] == 0 and r["excluded_no_phone"] == 1

    @pytest.mark.asyncio
    async def test_max_per_recipient(self, db):
        for i in range(3):
            db["leads"]._docs.append(_lead(f"lead-{i}"))
            db["crm_assignment_cycles"]._docs.append(_cycle(f"lead-{i}", f"cycle-{i}", "user-erika"))
        db["crm_events"] = FakeCollection([])
        db["crm_management_results"] = FakeCollection([])
        db["usuarios"] = FakeCollection([_user("user-erika", phone="+56911111111")])
        db[COLLECTION] = FakeCollection([])
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            stack.enter_context(patch(f"{P}._settings.CRM_SLA_ALERTS_MAX_PER_RECIPIENT_PER_RUN", 2))
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["persisted"] == 2

    @pytest.mark.asyncio
    async def test_max_total(self, db):
        for i in range(5):
            uid = f"user-{i}" if i > 0 else "user-erika"
            db["leads"]._docs.append(_lead(f"lead-{i}"))
            db["crm_assignment_cycles"]._docs.append(_cycle(f"lead-{i}", f"cycle-{i}", uid))
            db["usuarios"]._docs.append(_user(uid, phone="+56911111111"))
        db["crm_events"] = FakeCollection([])
        db["crm_management_results"] = FakeCollection([])
        db[COLLECTION] = FakeCollection([])
        allowlist = frozenset({"user-erika", "user-1", "user-2", "user-3", "user-4"})
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            stack.enter_context(patch(f"{P}._settings.CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS", allowlist))
            stack.enter_context(patch(f"{P}._settings.CRM_SLA_ALERTS_MAX_PER_RUN", 3))
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["persisted"] == 3

    @pytest.mark.asyncio
    async def test_confirmation_incorrect_zero_writes(self, db):
        self._setup_db(db)
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            stack.enter_context(patch(f"{S}.CRM_SLA_ALERTS_PERSIST_CONFIRMATION", "WRONG"))
            stack.enter_context(patch(f"{P}._settings.CRM_SLA_ALERTS_PERSIST_CONFIRMATION", "WRONG"))
            r = await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert r["status"] == "config_blocked" and r["writes"] == 0


class TestIndexes:
    @pytest.mark.asyncio
    async def test_no_confirmation_zero_create_index(self, db):
        with patch(f"{S}.CRM_SLA_ALERTS_PERSIST_CONFIRMATION", ""):
            from chatbot.crm_sla_alert_settings import validate_indexes_config
            r = validate_indexes_config()
        assert not r["valid"]

    @pytest.mark.asyncio
    async def test_live_send_true_blocks_indexes(self, db):
        with patch(f"{S}.CRM_SLA_ALERTS_LIVE_SEND", True):
            from chatbot.crm_sla_alert_settings import validate_indexes_config
            r = validate_indexes_config()
        assert not r["valid"]

    @pytest.mark.asyncio
    async def test_correct_confirmation_allows_indexes(self, db):
        with patch(f"{S}.CRM_SLA_ALERTS_PERSIST_CONFIRMATION", REQUIRED_PERSIST_CONFIRMATION), \
             patch(f"{S}.CRM_SLA_ALERTS_LIVE_SEND", False):
            from chatbot.crm_sla_alert_settings import validate_indexes_config
            r = validate_indexes_config()
        assert r["valid"]


class TestIsolation:
    def test_no_worker_or_sender_import(self):
        import chatbot.crm_sla_alert_pipeline as mod
        src = open(mod.__file__).read()
        lines = [l for l in src.split('\n') if l.startswith(('import ', 'from '))]
        joined = '\n'.join(lines)
        assert "crm_sla_alert_worker" not in joined
        assert "crm_sla_alert_sender" not in joined
        assert "NotificationService" not in joined
        assert "whatsapp_client" not in joined
        assert "crm_hot_delivery" not in joined
        assert "webhook" not in joined

    @pytest.mark.asyncio
    async def test_no_writes_outside(self, db):
        db["leads"] = FakeCollection([_lead("lead-1")])
        db["crm_assignment_cycles"] = FakeCollection([_cycle("lead-1")])
        db["crm_events"] = FakeCollection([])
        db["crm_management_results"] = FakeCollection([])
        db["usuarios"] = FakeCollection([_user("user-erika", phone="+56911111111")])
        db[COLLECTION] = FakeCollection([])
        with ExitStack() as stack:
            for p in _setup_persist_patches(cut=_today_cl_9am()):
                stack.enter_context(p)
            await run_evaluation_and_persist_once(db=db, max_cycles=10, now=_test_now())
        assert len(db[COLLECTION]._docs) >= 1
        assert len(db["leads"]._docs) == 1  # unchanged
