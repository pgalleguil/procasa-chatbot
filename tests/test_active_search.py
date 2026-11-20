from datetime import datetime, timedelta, timezone

import mongomock
from bson import ObjectId

from chatbot.active_search import capture_active_search, find_matching_inventory
from chatbot.active_search_followup import ensure_followup_task, refresh_active_search_batch

NOW = datetime(2026, 11, 18, 19, 30, tzinfo=timezone.utc)
CONV = "office-conv-1"
CYCLE = "cycle-hernan"
OWNER = "executive-hernan"
SESSION = "133517"


def office(text, n):
    return dict(role="user", actor_type="customer", content=text,
                conversation_id=CONV, message_id=f"office-{n}",
                timestamp=(NOW - timedelta(minutes=80-n)).isoformat())


def executive(text, n):
    return dict(role="user", actor_type="customer", content=text,
                channel="executive_whatsapp", source="executive_whatsapp",
                assignment_cycle_id=CYCLE, assigned_executive_id=OWNER,
                wasender_session_id=SESSION, message_id=f"exec-{n}",
                provider_message_id=f"provider-{n}",
                timestamp=(NOW - timedelta(minutes=40-n)).isoformat())


def classify(msg, prior=None):
    return capture_active_search(
        msg, conversation_id=CONV, cycle_id=CYCLE,
        owner_id=OWNER, session_id=SESSION,
        original_property_code="17184", original_operation="Venta",
        prior=prior,
    )


def test_diana_full_office_and_executive_chat_preserves_buyer_search():
    history = [
        office("ah no me sirve ya muchas gracias", 1),
        office("si porfa, ando buscando casa o departamento de 97 millones maximo", 2),
        office("en pudahuel o maipu", 3),
        office("casa porfis", 4),
        office("Super muchas gracias", 5),
        executive("Si pero no me sirve por precio", 6),
        executive("97 millones en pudahuel y maipu", 7),
        executive("gracias", 8),
    ]
    search = classify(history)
    assert search["status"] == "ACTIVE"
    assert search["followup_required"] is True
    assert search["criteria"]["max_price_clp"] == 97_000_000
    assert search["criteria"]["comunas"] == ["Pudahuel", "Maipú"]
    assert search["criteria"]["property_types"] == ["CASA", "DEPARTAMENTO"]
    assert search["criteria"]["preferred_property_type"] == "CASA"
    assert search["criteria"]["operation"] == "VENTA"
    assert "17184" in search["excluded_property_codes"]
    assert search["source_channels"] == ["office_whatsapp", "executive_whatsapp"]
    assert "max_price_clp" in search["field_evidence"]
    assert "gracias" not in str(search)


def test_short_executive_confirmation_updates_prior_office_search():
    prior = classify([
        office("ando buscando casa o departamento hasta 97 millones", 1),
        office("en pudahuel o maipu", 2),
    ])
    assert prior["criteria"]["property_types"] == ["CASA", "DEPARTAMENTO"]
    updated = classify([executive("casa", 3), executive("gracias", 4)], prior=prior)
    assert updated["status"] == "ACTIVE"
    assert updated["criteria"]["property_types"] == ["CASA", "DEPARTAMENTO"]
    assert updated["criteria"]["preferred_property_type"] == "CASA"
    assert updated["criteria"]["max_price_clp"] == 97_000_000
    assert updated["criteria"]["comunas"] == ["Pudahuel", "Maipú"]


def test_private_old_cycle_portal_bot_and_agent_text_cannot_supply_budget():
    now = NOW.isoformat()
    good = [office("Estoy buscando una casa", 1)]
    for fake in [
        {"role": "user", "actor_type": "customer", "channel": "executive_whatsapp",
         "assignment_cycle_id": "other-cycle", "assigned_executive_id": OWNER,
         "wasender_session_id": SESSION, "content": "97 millones en Pudahuel",
         "provider_message_id": "wrong", "timestamp": now},
        {"role": "user", "actor_type": "customer", "source": "prop360",
         "conversation_id": CONV, "content": "hasta 97 millones",
         "message_id": "portal", "timestamp": now},
        {"role": "assistant", "actor_type": "bot", "conversation_id": CONV,
         "content": "La propiedad cuesta $95 millones", "message_id": "bot", "timestamp": now},
        {"role": "user", "actor_type": "customer", "channel": "private",
         "content": "97 millones Maipú", "message_id": "private", "timestamp": now},
    ]:
        assert (classify(good + [fake])["criteria"].get("max_price_clp")) is None


def test_gratitude_is_not_search_end_but_explicit_completion_is():
    started = classify([office("busco casa de 97 millones maximo en Pudahuel", 1)])
    assert classify([office("gracias", 2)], prior=started)["status"] == "ACTIVE"
    ended = classify([office("Ya compré una casa", 3)], prior=started)
    assert ended["status"] == "COMPLETED"
    assert ended["followup_required"] is False


def test_explicit_stop_prohibits_followup_and_does_not_revoke_search_evidence():
    search = classify([office("busco casa hasta 97 millones en maipu", 1),
                       office("no me contacten mas", 2)])
    assert search["status"] == "ACTIVE"
    assert search["opt_out"] is True


def _property(code, *, price=91_000_000, comuna="Maipú", kind="Casa",
              published=True, office="PROCASA SUCRE", active=True):
    return {
        "codigo": code, "disponible_prop360": active,
        "estado": {"estado_prop360": "Activa", "oficina": office},
        "ubicacion": {"comuna": comuna},
        "tipo_operacion": {"tipo": kind, "venta": True,
                           "precio_venta": {"precio_clp": price, "precio_uf": None}},
        "publicaciones": {"procasa": {
            "url_procasa": f"https://www.procasa.cl/{code}",
            "publicaciones": {"V": {"publicada": published}},
        }},
    }


def test_matches_only_verified_active_local_budget_and_omits_declined_listing():
    db = mongomock.MongoClient().active_search
    search = classify([office("ando buscando casa hasta 97 millones en maipu", 1)])
    db.universo_cartera_prop360.insert_many([
        _property("1111", price=91_000_000),
        _property("2222", price=99_000_000),
        _property("3333", comuna="Peñaflor"),
        _property("4444", office="OTHER OFFICE"),
        _property("5555", published=False),
        _property("6666", active=False),
        _property("7777", kind="Departamento"),
        _property("17184"),
    ])
    search["excluded_property_codes"] = ["17184"]
    matches = find_matching_inventory(db, search)
    assert [x["property_code"] for x in matches] == ["1111"]
    assert matches[0]["verification"] == "RECHECK_PRICE_AND_AVAILABILITY"


def test_followup_task_once_then_new_inventory_creates_new_task_not_outbound():
    db = mongomock.MongoClient().active_search
    lead_id = ObjectId()
    db.leads.insert_one({
        "_id": lead_id, "phone": "+56911112222", "ejecutivo_asignado": "Hernán Castro",
        "lifecycle": {"assigned_to_user_id": OWNER,
                      "current_assignment_cycle_id": CYCLE},
        "commercial_intelligence": {"active_search": {}},
    })
    search = classify([office("busco casa hasta 97 millones en maipu", 1)])
    search.update(matches=[], match_status="WAITING_INVENTORY",
                  last_matched_at=(NOW - timedelta(hours=1)).isoformat())
    db.leads.update_one({"_id": lead_id}, {"$set": {
        "commercial_intelligence.active_search": search,
    }})
    first = ensure_followup_task(db, lead_id=lead_id, cycle_id=CYCLE,
                                 owner_id=OWNER, search=search, now=NOW)
    assert first
    assert first == ensure_followup_task(db, lead_id=lead_id, cycle_id=CYCLE,
                                        owner_id=OWNER, search=search, now=NOW)
    assert db.crm_tasks.count_documents({}) == 1
    assert "97.000.000" in db.crm_tasks.find_one({"_id": first})["note"]
    db.crm_assignment_cycles.insert_one({
        "lead_id": lead_id, "cycle_status": "active", "unassigned_at": None,
        "assignment_cycle_id": CYCLE, "assigned_to_user_id": OWNER,
    })
    db.universo_cartera_prop360.insert_one(_property("1234"))
    updated = refresh_active_search_batch(db, limit=10, now=NOW)
    assert updated["checked"] == 1
    assert db.crm_tasks.count_documents({}) == 2
    assert db.crm_tasks.find_one({"_id": first})["status"] == "completed"
    match_task = db.crm_tasks.find_one({"status": "pending"})
    assert match_task["type"] == "SEARCH_ALTERNATIVES"
    assert match_task["property_codes_to_review"] == ["1234"]
    assert "confirmar disponibilidad" in match_task["note"]
    assert db.leads.find_one({"_id": lead_id})["commercial_intelligence"]["active_search"]["match_status"] == "CANDIDATES_NEED_VERIFICATION"



def test_existing_diana_classification_is_repaired_without_new_customer_message(monkeypatch):
    from chatbot import executive_intelligence_worker as worker
    db = mongomock.MongoClient().repair
    lead_id = ObjectId()
    history = [
        office("No me sirve por precio", 1),
        office("ando buscando casa o departamento de 97 millones maximo", 2),
        office("en Pudahuel o Maipu", 3),
        office("casa", 4),
        office("gracias", 5),
        executive("97 millones en Pudahuel y Maipú", 6),
        executive("gracias", 7),
    ]
    db.leads.insert_one({
        "_id": lead_id, "phone": "+56911112222", "conversation_id": CONV,
        "codigo_propiedad": "17184", "operacion": "Venta",
        "ejecutivo_asignado": "Hernán Castro", "messages": history,
        "lifecycle": {"assigned_to_user_id": OWNER,
                      "current_assignment_cycle_id": CYCLE},
        "commercial_intelligence": {
            "pending": False, "pending_source": "executive_whatsapp",
            "pending_cycle_id": CYCLE, "pending_session_id": SESSION,
            "summary": {"search_status": "ACTIVE", "intent": "NO_SIGNAL",
                        "search": {}, "needs": {},
                        "contact_permission": "UNKNOWN",
                        "executive_management": {"case_type": "REQUISITOS_ACTUALIZADOS"}},
        },
    })
    monkeypatch.setattr(worker, "_is_authorized", lambda *_: True)
    assert db.leads.count_documents({
        "commercial_intelligence.summary.search_status": "ACTIVE",
        "commercial_intelligence.active_search": {"$exists": False},
        "commercial_intelligence.pending": False,
    }) == 1
    office_turns, source = worker._load_office_search_context(
        db, db.leads.find_one({"_id": lead_id}),
    )
    assert len(office_turns) >= 3, office_turns
    assert source["conversation_id"] == CONV
    result = worker.backfill_missing_active_searches(db, limit=2)
    assert result["repaired"] == 1
    saved = db.leads.find_one({"_id": lead_id})
    profile = saved["commercial_intelligence"]["active_search"]
    assert profile["status"] == "ACTIVE"
    assert profile["criteria"]["max_price_clp"] == 97_000_000
    assert profile["criteria"]["comunas"] == ["Pudahuel", "Maipú"]
    assert profile["criteria"]["property_types"] == ["CASA", "DEPARTAMENTO"]
    assert profile["criteria"]["preferred_property_type"] == "CASA"
    assert profile["match_status"] == "WAITING_INVENTORY"
    assert saved["commercial_intelligence"]["summary"]["followup_search_required"]
    assert saved["commercial_intelligence"]["summary"]["executive_management"]["case_type"] == "BUSQUEDA_ACTIVA_ALTERNATIVAS"
    assert db.crm_tasks.count_documents({"type": "SEARCH_ALTERNATIVES"}) == 1
    # An inventory refresh must not recreate the profile or duplicate tasks.
    assert worker.backfill_missing_active_searches(db, limit=2)["repaired"] == 0
    assert db.crm_tasks.count_documents({}) == 1

def test_karina_landmark_is_queued_without_inventing_budget_or_commune(monkeypatch):
    # Real buyer wording. A landmark is a human verification cue, not a
    # licence to infer geographical radius or scrape the wrong neighborhood.
    search = classify([
        executive("Pero me di cuenta que está más cerca del Pumahue", 1),
        executive("Busco algo más cerca del San Fco Javier", 2),
        executive("Tiene algo?", 3),
    ])
    assert search["status"] == "ACTIVE"
    assert search["followup_required"] is True
    assert search["criteria"]["location_targets"] == ["san fco javier"]
    assert "max_price_clp" not in search["criteria"]
    assert "comunas" not in search["criteria"]
    assert "pumahue" not in str(search["criteria"])
    assert search["field_evidence"]["location_targets"]["verification"] == "HUMAN_GEOCODING_REQUIRED"
    db = mongomock.MongoClient().landmark
    lead_id = ObjectId()
    db.leads.insert_one({
        "_id": lead_id, "ejecutivo_asignado": "Hernán Castro",
        "lifecycle": {"assigned_to_user_id": OWNER,
                      "current_assignment_cycle_id": CYCLE},
        "commercial_intelligence": {"active_search": search},
    })
    monkeypatch.setenv("PROCASA_DEMAND_SOURCING_ENABLED", "1")
    task_id = ensure_followup_task(db, lead_id=lead_id,
                                  cycle_id=CYCLE, owner_id=OWNER,
                                  search=search, now=NOW)
    task = db.crm_tasks.find_one({"_id": task_id})
    assert task["type"] == "SEARCH_ALTERNATIVES"
    assert "san fco javier" in task["note"]
    assert "presupuesto por confirmar" in task["note"]
    review = db.crm_tasks.find_one({"type": "DEMAND_SOURCING_ADMIN_REVIEW"})
    assert review["buyer_executive_name"] == "Hernán Castro"
    assert review["candidate_refs"] == []
    assert review["review_scope"] == "LOCATION_OR_BUDGET_NEEDS_HUMAN_VERIFICATION"
    assert review["contact_approved"] is False


def test_reference_question_cannot_override_office_preference():
    prior = classify([
        office("ando buscando casa o departamento de 97 millones maximo", 1),
        office("en pudahuel o maipu", 2),
        office("casa porfis", 3),
    ])
    preferred_source = prior["field_evidence"]["preferred_property_type"]
    updated = classify([executive("hola de que casa hablamos?", 4)], prior=prior)
    assert updated["criteria"]["property_types"] == ["CASA", "DEPARTAMENTO"]
    assert updated["criteria"]["preferred_property_type"] == "CASA"
    assert updated["field_evidence"]["preferred_property_type"] == preferred_source


def test_exclusive_type_is_a_hard_constraint():
    profile = classify([
        office("busco casa o departamento de 97 millones en Maipú", 1),
        office("solo casas", 2),
    ])
    assert profile["criteria"]["property_types"] == ["CASA"]
    assert profile["criteria"]["preferred_property_type"] == "CASA"

