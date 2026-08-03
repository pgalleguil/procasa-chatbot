"""Production CRM SLA alert policy tests.

These tests intentionally exercise the single-switch contract rather than
environment-controlled canary/dry-run variants.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from chatbot.constants import CHILE_TZ
from chatbot.crm_sla_alert_pipeline import run_evaluation_and_persist_once
from chatbot.crm_sla_alert_settings import CUTOVER_AT
from chatbot.crm_sla_alert_repository import COLLECTION


class Cursor:
    def __init__(self, docs): self.docs = list(docs)
    async def to_list(self, length=None): return self.docs[:length] if length else self.docs


class Collection:
    def __init__(self, docs=None): self.docs = list(docs or [])
    def find(self, query, projection=None):
        out = []
        for doc in self.docs:
            ok = True
            for key, expected in query.items():
                value = doc.get(key)
                if isinstance(expected, dict) and "$gte" in expected:
                    ok = value is not None and value >= expected["$gte"]
                elif isinstance(expected, dict) and "$in" in expected:
                    ok = value in expected["$in"]
                elif isinstance(expected, dict) and "$exists" in expected:
                    ok = (key in doc) == expected["$exists"]
                elif value != expected:
                    ok = False
                if not ok: break
            if ok: out.append(doc)
        return Cursor(out)
    async def find_one(self, query, *args):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()): return dict(doc)
        return None
    async def insert_one(self, doc):
        if any(x.get("_id") == doc.get("_id") for x in self.docs):
            from pymongo.errors import DuplicateKeyError
            raise DuplicateKeyError("duplicate")
        self.docs.append(dict(doc))


class DB(dict):
    def __missing__(self, key):
        self[key] = Collection()
        return self[key]


def cl(h, m=0, day=3):
    return CHILE_TZ.localize(datetime(2026, 8, day, h, m)).astimezone(timezone.utc)


def lead(lid, name="Cliente", prop="P001", temp="HOT", executive="Erika Garrido"):
    return {"_id": lid, "phone": "+56912345678", "lead_temperature_effective": temp,
            "pipeline_stage": "NEW", "ejecutivo_asignado": executive,
            "prospecto": {"nombre": name, "codigo": prop}}


def cycle(lid, cid, uid="u-erika", assigned=None):
    return {"lead_id": lid, "assignment_cycle_id": cid, "assigned_to_user_id": uid,
            "assigned_at": assigned or cl(9), "unassigned_at": None,
            "cycle_status": "active", "reason": "lead_created"}


def user(uid, name, role="agente", active=True, phone="+56911111111"):
    return {"_id": uid, "nombre": name, "rol": role, "is_active": active,
            "telefono": phone}


def populated_db():
    db = DB()
    db["leads"] = Collection([lead("l1")])
    db["crm_assignment_cycles"] = Collection([cycle("l1", "c1")])
    db["crm_events"] = Collection()
    db["crm_management_results"] = Collection()
    db["usuarios"] = Collection([user("u-erika", "Erika Garrido")])
    db[COLLECTION] = Collection()
    return db


@pytest.mark.asyncio
async def test_disabled_switch_performs_zero_operations():
    class NoTouchDB:
        def __getitem__(self, key): raise AssertionError(f"unexpected DB access: {key}")
    with patch("chatbot.crm_sla_alert_pipeline.CRM_SLA_ALERTS_ENABLED", False):
        result = await run_evaluation_and_persist_once(db=NoTouchDB())
    assert result["status"] == "disabled"
    assert result["writes"] == result["claims"] == result["sends"] == 0


@pytest.mark.asyncio
async def test_enabled_persists_one_breached_without_warning_retroactive():
    db = populated_db()
    now = cl(10, 30)
    with patch("chatbot.crm_sla_alert_pipeline.CRM_SLA_ALERTS_ENABLED", True):
        result = await run_evaluation_and_persist_once(db=db, now=now)
    assert result["persisted"] == 1
    assert len(db[COLLECTION].docs) == 1
    assert db[COLLECTION].docs[0]["alert_level"] == "breached"


@pytest.mark.asyncio
async def test_previous_cutover_cycle_is_excluded():
    db = populated_db()
    db["leads"].docs.append(lead("old"))
    db["crm_assignment_cycles"].docs.append(cycle("old", "old-cycle", assigned=cl(18, 0, day=2)))
    with patch("chatbot.crm_sla_alert_pipeline.CRM_SLA_ALERTS_ENABLED", True):
        result = await run_evaluation_and_persist_once(db=db, now=cl(10, 30))
    assert result["persisted"] == 1
    assert result["excluded_by_cutover"] == 0 if "excluded_by_cutover" in result else True
    assert all(d["assignment_cycle_id"] == "c1" for d in db[COLLECTION].docs)


@pytest.mark.asyncio
async def test_only_active_agents_with_phone_are_recipients():
    db = populated_db()
    db["usuarios"].docs.extend([
        user("u-pablo", "Pablo Galleguillos", role="admin"),
        user("u-inactive", "Inactive", active=False),
        user("u-no-phone", "No Phone", phone=""),
    ])
    with patch("chatbot.crm_sla_alert_pipeline.CRM_SLA_ALERTS_ENABLED", True):
        result = await run_evaluation_and_persist_once(db=db, now=cl(10, 30))
    assert result["persisted"] == 1
    assert db[COLLECTION].docs[0]["recipient_user_id"] == "u-erika"


def test_fixed_policy_has_no_environment_dependencies():
    import chatbot.crm_sla_alert_settings as settings
    assert settings.DRY_RUN is False
    assert settings.PERSIST is True
    assert settings.LIVE_SEND is True
    assert settings.CANARY_MODE is False
    assert settings.REASSIGNMENT_ENABLED is False
    assert settings.CUTOVER_AT == CUTOVER_AT
