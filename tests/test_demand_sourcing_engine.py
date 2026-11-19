from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import mongomock
import pytest
from bson import ObjectId
from fastapi import HTTPException
from starlette.datastructures import Headers, URL

from api_demand_sourcing import _require_same_origin
from chatbot.demand_sourcing_engine import (
    ALERT_COLLECTION,
    CANDIDATE_COLLECTION,
    CASE_COLLECTION,
    JOB_COLLECTION,
    ack_admin_alerts,
    deliver_candidate_to_executive,
    ensure_demand_case,
    ensure_indexes,
    index_external_listing,
    index_procasa_listing,
    missing_criteria,
    record_case_tracking,
    retry_search_job,
    review_candidate,
    run_one_job,
    update_case_criteria,
)


NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def criteria(*, budget=97_000_000, communes=None, kinds=None, targets=None):
    return {
        "operation": "VENTA",
        "max_price_clp": budget,
        "comunas": list(communes or ["Maipú", "Pudahuel"]),
        "property_types": list(kinds or ["CASA", "DEPARTAMENTO"]),
        "preferred_property_type": "CASA",
        "location_targets": list(targets or []),
    }


def search(**kwargs):
    return {"status": "ACTIVE", "followup_required": True,
            "criteria": criteria(**kwargs), "source_channels": ["whatsapp_executive"]}


def create_active_lead(db, profile=None, *, owner="exec-1", cycle="cycle-1"):
    lead_id = ObjectId()
    db.leads.insert_one({
        "_id": lead_id,
        "ejecutivo_asignado": "Ejecutivo de prueba",
        "lifecycle": {"assigned_to_user_id": owner,
                      "current_assignment_cycle_id": cycle},
        "commercial_intelligence": {"active_search": profile or search()},
    })
    db.crm_assignment_cycles.insert_one({
        "lead_id": lead_id, "unassigned_at": None, "cycle_status": "active",
        "assignment_cycle_id": cycle, "assigned_to_user_id": owner,
        "assigned_at": NOW,
    })
    return lead_id


def listing(listing_id="listing-1", *, portal="toctoc", commune="Maipú",
            price=95_000_000, kind="casa", address="", management=None):
    return {
        "listing_id": listing_id, "source_portal": portal,
        "url": f"https://{portal}.cl/propiedades/{listing_id}",
        "operacion": "venta", "tipo_propiedad": kind, "comuna": commune,
        "precio_clp_normalizado": price, "precio_validacion": "OK",
        "updated_at": NOW, "last_seen_at": NOW,
        "classification": {"state": "INCIERTO", "final_state": "INCIERTO"},
        "gestion": management or {}, "direccion_exacta": address,
    }


@pytest.fixture
def db(monkeypatch):
    database = mongomock.MongoClient().demand_test
    ensure_indexes(database)
    import broker_registry
    monkeypatch.setattr(
        broker_registry, "resolve_broker_identity_batch",
        lambda _db, docs, include_alias=False: [
            {"available": True, "matched": False, "conflict": False} for _ in docs
        ],
    )
    return database


def make_case(db, *, profile=None, owner="exec-1", cycle="cycle-1"):
    profile = profile or search()
    lead_id = create_active_lead(db, profile, owner=owner, cycle=cycle)
    case = ensure_demand_case(db, lead_id=lead_id, cycle_id=cycle,
                              owner_id=owner, search=profile, now=NOW)
    return lead_id, case


def test_ten_equivalent_buyers_share_one_job_and_keep_individual_budget_gates(db):
    cases = []
    for index in range(10):
        profile = search()
        lead_id = create_active_lead(db, profile, owner=f"exec-{index}", cycle=f"cycle-{index}")
        cases.append(ensure_demand_case(db, lead_id=lead_id, cycle_id=f"cycle-{index}",
                                        owner_id=f"exec-{index}", search=profile, now=NOW))

    assert db[CASE_COLLECTION].count_documents({}) == 10
    assert db[JOB_COLLECTION].count_documents({}) == 1
    shared_job = db[JOB_COLLECTION].find_one({})
    assert shared_job["lead_count"] == 10
    assert len(shared_job["case_ids"]) == 10

    # The strict price is part of the signature and every relation is checked
    # against its own case before it can be shown.
    tight_profile = search(budget=90_000_000)
    tight_lead = create_active_lead(db, tight_profile, owner="exec-tight", cycle="cycle-tight")
    tight_case = ensure_demand_case(db, lead_id=tight_lead, cycle_id="cycle-tight",
                                    owner_id="exec-tight", search=tight_profile, now=NOW)
    assert db[JOB_COLLECTION].count_documents({}) == 2

    index_external_listing(db, listing(price=95_000_000), observed_at=NOW, now=NOW)
    candidates = list(db[CANDIDATE_COLLECTION].find({"source_type": "EXTERNAL"}))
    assert len(candidates) == 10
    assert all(row["price_clp"] <= 97_000_000 for row in candidates)
    assert db[CANDIDATE_COLLECTION].count_documents({"case_id": tight_case["case_id"]}) == 0


def test_inverse_match_creates_three_relations_and_deduplicated_alerts(db):
    cases = [make_case(db, owner=f"exec-{n}", cycle=f"cycle-{n}")[1] for n in range(3)]
    property_doc = listing("inverse-1")
    index_external_listing(db, property_doc, observed_at=NOW, now=NOW)
    index_external_listing(db, property_doc, observed_at=NOW, now=NOW)

    assert db[CANDIDATE_COLLECTION].count_documents({"listing_id": "inverse-1"}) == 3
    assert db[ALERT_COLLECTION].count_documents({"alert_type": "NEW_COMPATIBLE_LISTING"}) == 3
    assert len({case["case_id"] for case in cases}) == 3
    pending = db[ALERT_COLLECTION].find({"state": "pending"})
    alert_keys = [row["alert_key"] for row in pending]
    assert ack_admin_alerts(db, alert_keys=alert_keys, actor="admin-1", now=NOW)["sent"] == len(alert_keys)
    assert db[ALERT_COLLECTION].count_documents({"state": "sent"}) == len(alert_keys)


def test_repeated_webhook_updates_one_case_job_and_alert(db):
    profile = search()
    lead_id = create_active_lead(db, profile)
    case_ids = []
    for _ in range(5):
        case = ensure_demand_case(db, lead_id=lead_id, cycle_id="cycle-1",
                                  owner_id="exec-1", search=profile, now=NOW)
        case_ids.append(case["case_id"])

    assert len(set(case_ids)) == 1
    assert db[CASE_COLLECTION].count_documents({}) == 1
    assert db[JOB_COLLECTION].count_documents({}) == 1
    assert db[ALERT_COLLECTION].count_documents({}) == 1


def test_buyer_excluded_procasa_code_is_a_case_revision_veto(db):
    profile = search()
    profile["excluded_property_codes"] = ["17184"]
    _lead_id, case = make_case(db, profile=profile)
    source = {
        "codigo": "17184",
        "tipo_operacion": {"venta": True, "tipo": "casa",
                           "precio_venta": {"precio_clp": 95_000_000, "moneda_publicada": "CLP"}},
        "disponible_prop360": True,
        "estado": {"estado_prop360": "activa", "oficina": "PROCASA SUCRE"},
        "publicaciones": {"procasa": {"publicaciones": {"V": {"publicada": True}}}},
        "ubicacion": {"comuna": "Maipú"}, "updated_at": NOW,
    }
    index_procasa_listing(db, source, now=NOW)
    candidate = db[CANDIDATE_COLLECTION].find_one({"case_id": case["case_id"], "property_code": "17184"})
    assert candidate["status"] == "EXCLUDED"
    assert candidate["exclusion_reason"] == "BUYER_EXCLUDED_PROPERTY"


def test_stale_procasa_stock_requires_human_price_and_availability_revalidation(db):
    _lead_id, case = make_case(db)
    source = {
        "codigo": "internal-stale",
        "tipo_operacion": {"venta": True, "tipo": "casa",
                           "precio_venta": {"precio_clp": 95_000_000, "moneda_publicada": "CLP"}},
        "disponible_prop360": True,
        "estado": {"estado_prop360": "activa", "oficina": "PROCASA SUCRE"},
        "publicaciones": {"procasa": {"publicaciones": {"V": {"publicada": True}}}},
        "ubicacion": {"comuna": "Maipú"},
        "updated_at": datetime(2026, 11, 10, tzinfo=timezone.utc),
    }
    db["universo_cartera_prop360"].insert_one(source)
    index_procasa_listing(db, source, now=NOW)
    candidate = db[CANDIDATE_COLLECTION].find_one({"case_id": case["case_id"],
                                                   "property_code": "internal-stale"})
    assert candidate["status"] == "PROCASA_PENDING_PRICE"
    verified = review_candidate(db, candidate_id=candidate["candidate_id"],
                                action="VERIFY_INTERNAL_STOCK", actor="admin-1", now=NOW,
                                evidence={"availability_confirmed": True, "price_confirmed": True,
                                          "verified_price_clp": 95_000_000,
                                          "verification_note": "Ficha activa y precio revisado."})
    assert verified["status"] == "PROCASA_VERIFICADO"
    task = deliver_candidate_to_executive(db, candidate_id=candidate["candidate_id"],
                                          actor="admin-1", now=NOW)
    stored = db.crm_tasks.find_one({"_id": task["task_id"]})
    assert stored["external_outbound_enabled"] is False


def test_uf_price_with_stale_conversion_is_not_a_strict_match(db):
    _lead_id, case = make_case(db)
    doc = listing("stale-uf", price=95_000_000)
    doc.update({"precio_uf": 2400, "moneda_publicada": "UF",
                "precio_conversion_source": "calculated_from_uf",
                "uf_fecha": datetime(2026, 11, 15, tzinfo=timezone.utc)})
    index_external_listing(db, doc, observed_at=NOW, now=NOW)
    catalog = db.demand_property_index.find_one({"listing_id": "stale-uf"})
    candidate = db[CANDIDATE_COLLECTION].find_one({"case_id": case["case_id"], "listing_id": "stale-uf"})
    assert catalog["price_clp"] is None
    assert candidate["status"] == "EXTERNAL_PENDING_PRICE"
    assert candidate["eligible_exact"] is False


@pytest.mark.parametrize("change", ["criteria", "assignment", "close"])
def test_stale_job_results_are_not_attached_after_criteria_or_cycle_change(db, change):
    lead_id, case = make_case(db)
    job = db[JOB_COLLECTION].find_one({"case_ids": case["case_id"]})

    def mutate_during_scrape(_source, _job):
        if change == "criteria":
            update_case_criteria(db, case_id=case["case_id"],
                                 patch={"max_price_clp": 80_000_000},
                                 actor="admin-1", now=NOW)
        elif change == "assignment":
            db.leads.update_one({"_id": lead_id}, {"$set": {
                "lifecycle.assigned_to_user_id": "exec-new",
                "lifecycle.current_assignment_cycle_id": "cycle-new",
            }})
            db.crm_assignment_cycles.update_one({"assignment_cycle_id": "cycle-1"}, {
                "$set": {"cycle_status": "closed", "unassigned_at": NOW},
            })
            db.crm_assignment_cycles.insert_one({
                "lead_id": lead_id, "unassigned_at": None, "cycle_status": "active",
                "assignment_cycle_id": "cycle-new", "assigned_to_user_id": "exec-new",
                "assigned_at": NOW,
            })
        else:
            record_case_tracking(db, case_id=case["case_id"], stage="CLOSED_LOST",
                                 actor="admin-1", now=NOW)
        return {"status": "completed", "records": [listing(f"stale-{change}", price=95_000_000)]}

    result = run_one_job(db, worker_id="test-worker", now=NOW,
                         scraper_runner=mutate_during_scrape, external_enabled=True)
    assert result["status"] == "completed"
    assert db[CANDIDATE_COLLECTION].count_documents({"listing_id": f"stale-{change}"}) == 0
    assert db[JOB_COLLECTION].find_one({"job_id": job["job_id"]})["status"] == "completed"
    if change == "close":
        assert db[CASE_COLLECTION].find_one({"case_id": case["case_id"]})["status"] == "RESUELTA"


def test_assignment_change_blocks_review_and_internal_delivery(db):
    lead_id, case = make_case(db)
    index_external_listing(db, listing("existing-1"), observed_at=NOW, now=NOW)
    candidate = db[CANDIDATE_COLLECTION].find_one({"case_id": case["case_id"]})
    assert candidate
    db.leads.update_one({"_id": lead_id}, {"$set": {
        "lifecycle.assigned_to_user_id": "exec-new",
        "lifecycle.current_assignment_cycle_id": "cycle-new",
    }})
    db.crm_assignment_cycles.update_one({"assignment_cycle_id": "cycle-1"}, {
        "$set": {"cycle_status": "closed", "unassigned_at": NOW},
    })
    db.crm_assignment_cycles.insert_one({
        "lead_id": lead_id, "unassigned_at": None, "cycle_status": "active",
        "assignment_cycle_id": "cycle-new", "assigned_to_user_id": "exec-new",
        "assigned_at": NOW,
    })
    with pytest.raises(ValueError, match="buyer_assignment_changed"):
        review_candidate(db, candidate_id=candidate["candidate_id"],
                         action="PRIORITIZE", actor="admin-1", now=NOW)
    with pytest.raises(ValueError, match="buyer_assignment_changed"):
        deliver_candidate_to_executive(db, candidate_id=candidate["candidate_id"],
                                       actor="admin-1", now=NOW)


def test_esquilo_cross_portal_human_broker_veto_blocks_uncertain_duplicate(db):
    _lead_id, case = make_case(db)
    owner_record = listing("112885892", portal="chilepropiedades", price=75_000_000,
                           address="Esquilo 2706",
                           management={"estado_captacion": "Corredor"})
    owner_record["url"] = "https://chilepropiedades.cl/ver-publicacion/112885892"
    uncertain_record = listing("4110707", portal="toctoc", price=75_000_000,
                               address="Esquilo 2706")
    index_external_listing(db, owner_record, observed_at=NOW, now=NOW)
    index_external_listing(db, uncertain_record, observed_at=NOW, now=NOW)

    duplicate = db[CANDIDATE_COLLECTION].find_one({"case_id": case["case_id"], "listing_id": "4110707"})
    assert duplicate["status"] == "EXCLUDED"
    assert duplicate["exclusion_reason"] == "HUMAN_BROKER_OR_CONTACT_VETO"
    assert not db[CANDIDATE_COLLECTION].count_documents({"listing_id": "4110707", "status": "EXTERNAL_PENDING_IDENTITY"})


def test_unconfirmed_school_reference_stays_visible_for_criteria_review(db):
    profile = {"status": "ACTIVE", "followup_required": True,
               "criteria": {"operation": "VENTA", "location_targets": ["San Fco Javier", "Pumahue"]}}
    _lead_id, case = make_case(db, profile=profile)

    assert case["status"] == "REQUIERE_DATOS"
    assert "max_price_clp" in case["missing_criteria"]
    assert "location_target_confirmation" in case["missing_criteria"]
    assert db[JOB_COLLECTION].count_documents({}) == 0
    alert = db[ALERT_COLLECTION].find_one({"case_id": case["case_id"]})
    assert alert["alert_type"] == "NEEDS_CRITERIA_REVIEW"
    assert "presupuesto por confirmar" not in json.dumps(alert["payload"]).lower()
    assert missing_criteria(case["criteria"])


def test_confirmed_location_does_not_match_unverified_listing_coordinates(db):
    _lead_id, case = make_case(db)
    case = update_case_criteria(db, case_id=case["case_id"], actor="admin-1", now=NOW,
                                patch={"location_targets": ["San Fco Javier"],
                                       "verified_location": {"latitude": -33.5, "longitude": -70.7,
                                                             "radius_m": 2000, "source": "admin pin"}})
    property_doc = listing("geo-unverified")
    property_doc["latitude"], property_doc["longitude"] = -33.501, -70.701
    index_external_listing(db, property_doc, observed_at=NOW, now=NOW)
    candidate = db[CANDIDATE_COLLECTION].find_one({"case_id": case["case_id"], "listing_id": "geo-unverified"})
    assert candidate["status"] == "LOCATION_REVIEW_PENDING"
    with pytest.raises(ValueError, match="external_listing_no_longer_matches"):
        review_candidate(db, candidate_id=candidate["candidate_id"], action="VERIFY_AUTHORIZATION",
                         actor="admin-1", now=NOW, evidence={
            "owner_identity_confirmed": True, "availability_confirmed": True,
            "price_confirmed": True, "commercial_authorization_confirmed": True,
            "verified_price_clp": 95_000_000, "verification_note": "Verificado.",
        })


def test_external_provider_failure_degrades_in_worker_without_losing_case(db):
    _lead_id, case = make_case(db)

    def offline(_source, _job):
        raise TimeoutError("provider unavailable")

    result = run_one_job(db, worker_id="offline-test", now=NOW,
                         scraper_runner=offline, external_enabled=True)
    assert result["status"] == "failed_retryable"
    assert db[CASE_COLLECTION].find_one({"case_id": case["case_id"]})
    job = db[JOB_COLLECTION].find_one({"job_id": result["job_id"]})
    assert job["last_error_code"] == "external_source_failed"
    retried = retry_search_job(db, job_id=result["job_id"], actor="admin-1", now=NOW)
    assert retried["status"] == "queued"
    assert retried["attempts"] == 1


def test_finished_shared_job_is_reused_without_leaving_a_new_case_searching(db):
    _lead_id, first_case = make_case(db)
    finished = run_one_job(db, worker_id="reuse-worker", now=NOW,
                           external_enabled=False)
    assert finished["status"] == "completed_degraded"
    profile = search()
    second_lead = create_active_lead(db, profile, owner="exec-second", cycle="cycle-second")
    second_case = ensure_demand_case(db, lead_id=second_lead, cycle_id="cycle-second",
                                     owner_id="exec-second", search=profile, now=NOW)

    assert db[JOB_COLLECTION].count_documents({}) == 1
    current = db[CASE_COLLECTION].find_one({"case_id": second_case["case_id"]})
    assert current["latest_job_id"] == finished["job_id"]
    assert current["status"] == "NUEVA"
    assert current["last_job_status"] == "completed_degraded"
    assert db[CASE_COLLECTION].find_one({"case_id": first_case["case_id"]})["status"] == "NUEVA"


def test_admin_discard_veto_persists_until_a_new_case_revision(db):
    _lead_id, case = make_case(db)
    property_doc = listing("discard-me")
    index_external_listing(db, property_doc, observed_at=NOW, now=NOW)
    candidate = db[CANDIDATE_COLLECTION].find_one({"case_id": case["case_id"]})
    review_candidate(db, candidate_id=candidate["candidate_id"], action="DISCARD",
                     actor="admin-1", now=NOW)
    index_external_listing(db, property_doc, observed_at=NOW, now=NOW)
    candidate = db[CANDIDATE_COLLECTION].find_one({"candidate_id": candidate["candidate_id"]})
    assert candidate["status"] == "EXCLUDED"
    assert candidate["decision"] == "DISCARD"

    update_case_criteria(db, case_id=case["case_id"], actor="admin-1", now=NOW,
                         patch={"max_price_clp": 96_000_000})
    index_external_listing(db, property_doc, observed_at=NOW, now=NOW)
    candidate = db[CANDIDATE_COLLECTION].find_one({"candidate_id": candidate["candidate_id"]})
    assert candidate["case_revision"] == 2
    assert candidate["status"] == "EXTERNAL_PENDING_IDENTITY"
    assert candidate["decision"] is None


def test_end_to_end_internal_delivery_requires_human_authorization_and_sends_nothing(db):
    _lead_id, case = make_case(db)
    assert db[ALERT_COLLECTION].find_one({"case_id": case["case_id"], "state": "pending"})

    result = run_one_job(
        db, worker_id="e2e-worker", now=NOW, external_enabled=True,
        scraper_runner=lambda _source, _job: {
            "status": "completed", "records": [listing("e2e-1", price=95_000_000)]},
    )
    assert result["status"] == "completed"
    candidate = db[CANDIDATE_COLLECTION].find_one({"case_id": case["case_id"]})
    assert candidate["status"] == "EXTERNAL_PENDING_IDENTITY"
    with pytest.raises(ValueError, match="external_candidate_not_authorized"):
        deliver_candidate_to_executive(db, candidate_id=candidate["candidate_id"],
                                       actor="admin-1", now=NOW)

    review_candidate(db, candidate_id=candidate["candidate_id"], action="VERIFY_AUTHORIZATION",
                     actor="admin-1", now=NOW, evidence={
        "owner_identity_confirmed": True, "availability_confirmed": True,
        "price_confirmed": True, "commercial_authorization_confirmed": True,
        "verified_price_clp": 95_000_000,
        "verification_note": "Identidad y autorización verificadas en llamada interna.",
    })
    task = deliver_candidate_to_executive(db, candidate_id=candidate["candidate_id"],
                                          actor="admin-1", now=NOW)
    assert task["status"] == "pending"
    stored = db.crm_tasks.find_one({"_id": task["task_id"]})
    assert stored["external_outbound_enabled"] is False
    assert stored["contact_approved"] is False
    assert db.messages.count_documents({}) == 0
    assert db.crm_tasks.count_documents({}) == 1


def test_admin_api_rejects_missing_auth_and_cross_origin_mutations(monkeypatch):
    import api_crm_weekly_report

    request_scope = {"type": "http", "method": "GET", "scheme": "http",
                     "server": ("procasa.test", 80), "client": ("127.0.0.1", 1),
                     "root_path": "", "path": "/", "query_string": b"",
                     "headers": []}
    request = api_crm_weekly_report.Request(request_scope)
    with pytest.raises(HTTPException) as missing:
        asyncio.run(api_crm_weekly_report.admin_user(request))
    assert missing.value.status_code == 401

    class UserRows:
        async def find_one(self, _query):
            return {"username": "operator", "rol": "ejecutivo"}

    class FakeDB:
        def __getitem__(self, name):
            assert name == "usuarios"
            return UserRows()

    monkeypatch.setattr(api_crm_weekly_report.jwt, "decode", lambda *_a, **_k: {"sub": "operator"})
    monkeypatch.setattr(api_crm_weekly_report, "get_async_db", lambda: FakeDB())
    request_scope["headers"] = [(b"cookie", b"access_token=token")]
    with pytest.raises(HTTPException) as forbidden:
        asyncio.run(api_crm_weekly_report.admin_user(api_crm_weekly_report.Request(request_scope)))
    assert forbidden.value.status_code == 403

    class CrossOriginRequest:
        headers = Headers({"origin": "https://attacker.example"})
        url = URL("https://procasa.test/api/crm/demand-sourcing/cases/x/criteria")

    with pytest.raises(HTTPException) as csrf:
        _require_same_origin(CrossOriginRequest())
    assert csrf.value.status_code == 403


def test_yapo_adapter_builds_bounded_demand_queries_and_cleans_batch_reports(monkeypatch):
    from chatbot import demand_sourcing_scrapers as adapters

    job = {"criteria_envelope": {"operation": "VENTA", "comunas": ["Maipú"],
                                 "property_types": ["CASA"], "max_price_clp": 97_000_000},
           "cost_controls": {"max_pages_per_source": 2, "max_urls_per_source": 30,
                             "max_records_per_source": 20}}
    commands = []

    def fake_run(command, *, timeout):
        commands.append(command)
        batch_id = command[command.index("--batch-id") + 1]
        reports = adapters.ROOT / "scraper" / "reports"
        reports.mkdir(exist_ok=True, parents=True)
        result_path = reports / f"processed_{batch_id}.json"
        result_path.write_text(json.dumps([{"listing_id": "mock-1", "comuna": "Maipú"}]), encoding="utf-8")
        return json.dumps({"processed_path": str(result_path)})

    monkeypatch.setattr(adapters, "_run", fake_run)
    output = adapters.run_existing_scraper("yapo", job)
    command = commands[0]
    assert "--start-url" in command
    assert "casa" in command[command.index("--start-url") + 1]
    assert "maip%c3%ba" in command[command.index("--start-url") + 1].lower()
    assert "--max-pages" in command and "--max-urls" in command
    assert "--dry-run" in command and "--no-llm" in command and "--skip-reports" in command
    assert output["coverage"] == {"requested_segments": 1, "searched_segments": 1, "omitted_segments": 0}
    assert output["records"][0]["listing_id"] == "mock-1"


def test_toctoc_adapter_uses_existing_detail_price_filter_and_explicit_batch(monkeypatch):
    from chatbot import demand_sourcing_scrapers as adapters

    job = {"criteria_envelope": {"operation": "VENTA", "comunas": ["Maipú"],
                                 "property_types": ["CASA"], "max_price_clp": 97_000_000},
           "cost_controls": {"max_pages_per_source": 2, "max_urls_per_source": 30,
                             "max_records_per_source": 20}}
    captured = []

    def fake_run(command, *, timeout):
        captured.extend(command)
        batch_id = command[command.index("--batch-id") + 1]
        reports = adapters.ROOT / "scrapers" / "scraper_toctoc" / "reports"
        reports.mkdir(exist_ok=True, parents=True)
        result_path = reports / f"processed_{batch_id}.json"
        result_path.write_text("[]", encoding="utf-8")
        return f"Processed 0 records -> {result_path}"

    monkeypatch.setattr(adapters, "_run", fake_run)
    output = adapters.run_existing_scraper("toctoc", job)
    assert "--batch-id" in captured and captured[captured.index("--batch-id") + 1].startswith("demand_")
    assert "--precio-hasta" in captured and "97000000" in captured
    assert "--dry-run" in captured and "--disable-post-distribution" in captured
    assert output["coverage"]["searched_segments"] == 1
