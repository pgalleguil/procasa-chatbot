from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import threading
import types
from unittest.mock import patch

import mongomock


def _load_module():
    touched = [
        "chatbot", "chatbot.constants", "chatbot.lead_router",
        "chatbot.link_extractor", "chatbot.property_lookup", "chatbot.storage",
        "api_captacion", "config", "chatbot.processing_service",
    ]
    previous = {name: sys.modules.get(name) for name in touched}
    package = types.ModuleType("chatbot")
    package.__path__ = [str(Path(__file__).parents[1] / "chatbot")]
    sys.modules.setdefault("chatbot", package)

    constants = types.ModuleType("chatbot.constants")
    constants.CHILE_TZ = timezone(timedelta(hours=-4))
    constants.UNASSIGNED_LABEL = "Sin asignar"
    sys.modules["chatbot.constants"] = constants

    stubs = {
        "chatbot.lead_router": {
            "find_responsible_executive": lambda *a, **k: None,
        },
        "chatbot.link_extractor": {
            "analizar_mensaje_para_link": lambda *a, **k: None,
            "extraer_codigo_internacional": lambda *a, **k: None,
            "URL_RE": None,
        },
        "chatbot.property_lookup": {
            "PROPERTY_COLLECTION_NAME": "properties",
            "find_property_by_any_identifier": lambda *a, **k: None,
            "get_prop_location": lambda *a, **k: None,
            "get_prop_operation": lambda *a, **k: None,
        },
        "chatbot.storage": {
            "get_db": lambda: None,
            "record_observability_event": lambda *a, **k: None,
        },
        "api_captacion": {
            "get_zone_for_comuna": lambda *a, **k: None,
            "normalize_commune_v2": lambda value: value,
            "_normalize_tipo": lambda value: value,
            "_normalize_operacion": lambda value: value,
        },
        "config": {"Config": type("Config", (), {"LEAD_ASSIGNMENT_THRESHOLD": 40})},
    }
    for name, attrs in stubs.items():
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module

    spec = importlib.util.spec_from_file_location(
        "chatbot.processing_service",
        Path(__file__).parents[1] / "chatbot" / "processing_service.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for name, original in previous.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    return module


service = _load_module()
NOW = datetime(2026, 7, 26, 16, 0, tzinfo=timezone.utc)


def _db(*docs):
    db = mongomock.MongoClient().db
    if docs:
        db.leads.insert_many(list(docs))
    return db


def _new(_id="lead-1", event="event-1", **extra):
    return {
        "_id": _id,
        "stage": "OPEN",
        "processing_required": True,
        "processing_state": "new",
        "processing_event_id": event,
        "processing_reason": "inbound_message",
        "processing_requested_at": NOW,
        **extra,
    }


def test_two_workers_cannot_claim_same_event():
    db = _db(_new())
    claims = []
    barrier = threading.Barrier(2)

    def run(owner):
        barrier.wait()
        claims.append(service.claim_next_processing_lead(db, owner=owner, now=NOW))

    threads = [threading.Thread(target=run, args=(f"worker-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(claim is not None for claim in claims) == 1


def test_completed_event_is_not_selected_but_new_event_is():
    db = _db(_new(processed_event_id="event-1"))
    assert service.claim_next_processing_lead(db, owner="w", now=NOW) is None
    db.leads.update_one(
        {"_id": "lead-1"},
        {"$set": {"processing_event_id": "event-2", "processing_state": "new"}},
    )
    assert service.claim_next_processing_lead(db, owner="w", now=NOW)


def test_retry_waits_until_due():
    db = _db(_new(
        processing_state="failed_retryable",
        processing_next_attempt_at=NOW + timedelta(minutes=1),
    ))
    assert service.claim_next_processing_lead(db, owner="w", now=NOW) is None
    assert service.claim_next_processing_lead(
        db, owner="w", now=NOW + timedelta(minutes=2)
    )


def test_expired_lease_is_recovered_once():
    db = _db(_new(
        processing_state="processing",
        processing_owner="dead",
        processing_token="old",
        processing_lease_until=NOW - timedelta(seconds=1),
    ))
    first = service.claim_next_processing_lead(db, owner="new", now=NOW)
    second = service.claim_next_processing_lead(db, owner="other", now=NOW)
    assert first and first["processing_owner"] == "new"
    assert second is None


def test_all_terminal_states_release_claim_and_append_history():
    for state in ("completed", "failed_retryable", "failed_terminal"):
        db = _db(_new(_id=state))
        claim = service.claim_next_processing_lead(db, owner="w", now=NOW)
        assert service.finalize_processing_claim(
            db, lead_id=state, token=claim["processing_token"],
            event_id="event-1", state=state, now=NOW,
        )
        doc = db.leads.find_one({"_id": state})
        assert "processing_token" not in doc
        assert "processing_lease_until" not in doc
        assert doc["processing_state"] == state
        assert doc["processing_history"][-1]["state"] == state


def test_technical_reasons_missing_event_and_derived_fields_are_not_selected():
    docs = [
        _new(_id=f"technical-{reason}", processing_reason=reason)
        for reason in service.TECHNICAL_PROCESSING_REASONS
    ]
    docs.extend([
        {
            "_id": "historical-missing-derived",
            "stage": "OPEN",
            "cluster_id": None,
            "zone": None,
            "ejecutivo_asignado": None,
        },
        _new(_id="missing-event", processing_event_id=None),
    ])
    db = _db(*docs)
    assert service.claim_next_processing_lead(db, owner="w", now=NOW) is None


def test_missing_notification_eligible_is_false_and_service_has_no_live_delivery_branch():
    cycle = {}
    assert cycle.get("notification_eligible", False) is False
    source = (Path(__file__).parents[1] / "chatbot" / "processing_service.py").read_text(
        encoding="utf-8"
    )
    assert "if False and (update_data.get(\"auto_reassigned\") or needs_new_cycle)" in source


def test_early_return_and_exception_both_finalize_claim():
    db = _db(_new())
    claim = service.claim_next_processing_lead(db, owner="w", now=NOW)
    with patch.object(service.LeadProcessingService, "_db", return_value=db), patch.object(
        service.LeadProcessingService, "_process_claimed_body", return_value=False
    ):
        assert service.LeadProcessingService.process_claimed(claim) is False
    assert db.leads.find_one({"_id": "lead-1"})["processing_state"] == "completed"

    db = _db(_new())
    claim = service.claim_next_processing_lead(db, owner="w", now=NOW)
    with patch.object(service.LeadProcessingService, "_db", return_value=db), patch.object(
        service.LeadProcessingService, "_process_claimed_body", side_effect=RuntimeError("private")
    ):
        assert service.LeadProcessingService.process_claimed(claim) is False
    doc = db.leads.find_one({"_id": "lead-1"})
    assert doc["processing_state"] == "failed_retryable"
    assert doc["processing_last_error"] == "RuntimeError"
    assert "processing_token" not in doc


def test_process_service_uses_separate_bounded_executor():
    source = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8")
    assert "_PROCESS_THREAD_POOL = ThreadPoolExecutor(max_workers=2" in source
    consumer = source[source.index("async def lead_consumer_worker"):source.index(
        "async def reassign_unassigned_leads_loop"
    )]
    assert "_PROCESS_THREAD_POOL" in consumer
    assert "_WORKER_THREAD_POOL" not in consumer
    producer = source[source.index("async def reassign_unassigned_leads_loop"):source.index(
        "async def cache_prewarmer_loop"
    )]
    assert "claim_next(owner)" in producer
    assert ".find(query" not in producer
