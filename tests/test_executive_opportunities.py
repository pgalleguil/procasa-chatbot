"""Evidence lifecycle tests for executive WhatsApp opportunity history."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from chatbot.executive_conversation_intelligence import classify_hybrid
from chatbot.executive_opportunities import merge_opportunities, OPEN


OWNER = "69796bc4bbebf240378eb739"
CYCLE = "cycle-A"
SESSION = "133286"
BASE = datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc)


def turn(actor, text, n, *, cycle=CYCLE, session=SESSION, owner=OWNER, context=False):
    return {
        "actor_type": actor, "content": text, "channel": "executive_whatsapp",
        "message_id": f"id-{n}", "provider_message_id": f"provider-{n}",
        "timestamp": (BASE + timedelta(minutes=n)).isoformat(),
        "assignment_cycle_id": cycle, "assigned_executive_id": owner,
        "wasender_session_id": session, "_context_only": context,
    }


def update(messages, ledger=None, summary=None, cycle=CYCLE):
    if summary is None:
        summary = classify_hybrid(messages, llm_enabled=False)
    return merge_opportunities(messages, ledger or [], summary, cycle_id=cycle,
                               session_id=SESSION, owner_id=OWNER)


def test_pending_visit_survives_greeting_and_same_cycle_followup():
    initial = turn("customer", "Quiero agendar una visita", 1)
    ledger, active, count = update([initial])
    assert count == 1
    assert active["case_type"] == "VISITA_SOLICITADA"
    assert active["status"] == "PENDIENTE_GESTION"
    assert active["evidence_message_ids"] == ["provider-1"]
    assert active["formal_crm_management"] is False
    original_id = active["opportunity_id"]

    thanks = turn("customer", "Muchas gracias", 2)
    ledger2, active2, count2 = update([thanks], ledger)
    assert count2 == 1
    assert len(ledger2) == 1
    assert active2["opportunity_id"] == original_id
    assert active2["status"] == "PENDIENTE_GESTION"


def test_agent_acknowledgement_is_not_visit_confirmed_or_formal_crm_management():
    visit = turn("customer", "Quiero agendar una visita", 1)
    ledger, _, _ = update([visit])
    reply = turn("human_agent", "Perfecto, lo reviso", 2)
    ledger, active, count = update([reply], ledger)
    assert count == 1
    assert active["agent_reply_observed"] is True
    assert active["agent_reply_message_id"] == "provider-2"
    assert active["status"] == "PENDIENTE_GESTION"
    assert active["formal_crm_management"] is False
    assert active.get("closed_at") is None


def test_repeated_requests_correlate_to_one_case_without_duplication():
    first = turn("customer", "Quiero agendar una visita", 1)
    again = turn("customer", "Quiero agendar una visita", 2)
    ledger, _, _ = update([first, again])
    assert len(ledger) == 1
    assert ledger[0]["evidence_message_ids"] == ["provider-1", "provider-2"]
    repeated, _, _ = update([first, again], ledger)
    assert repeated == ledger
    assert len(repeated) == 1


def test_explicit_cancellation_closes_only_matching_visit_not_offer():
    visit = turn("customer", "Quiero agendar una visita", 1)
    offer = turn("customer", "Mi oferta sería 2700 UF", 2)
    ledger, _, _ = update([visit, offer])
    cancel = turn("customer", "Cancela la visita", 3)
    after, active, count = update([cancel], ledger)
    by_kind = {o["case_type"]: o for o in after}
    assert by_kind["VISITA_SOLICITADA"]["status"] == "DESCARTADA"
    assert by_kind["VISITA_SOLICITADA"]["closed_evidence_message_id"] == "provider-3"
    assert by_kind["OFERTA_PROPUESTA"]["status"] == "PENDIENTE_GESTION"
    assert active["case_type"] == "OFERTA_PROPUESTA"
    assert count == 1
    replay, _, _ = update([visit, offer, cancel], after)
    assert replay == after


def test_new_visit_after_explicit_cancellation_creates_distinct_case():
    first = turn("customer", "Quiero agendar una visita", 1)
    cancel = turn("customer", "Cancela la visita", 2)
    again = turn("customer", "Quiero agendar una visita", 3)
    ledger, _, count = update([first, cancel, again])
    assert count == 1
    assert len(ledger) == 2
    assert [o["status"] for o in ledger] == ["DESCARTADA", "PENDIENTE_GESTION"]
    assert ledger[0]["opportunity_id"] != ledger[1]["opportunity_id"]


def test_another_cycle_keeps_history_but_never_inherits_old_open_case():
    visit = turn("customer", "Quiero agendar una visita", 1)
    old, active, _ = update([visit])
    other = turn("customer", "Hola", 4, cycle="cycle-B")
    ledger, active_new, count = update([other], old, cycle="cycle-B")
    assert ledger == old
    assert count == 0 and active_new is None
    assert active["assignment_cycle_id"] == CYCLE


def test_outgoing_offers_hypotheticals_and_private_text_cannot_create_cases():
    messages = [
        turn("human_agent", "Quiero agendar una visita", 1),
        turn("customer", "Si quisiera visitar, con quién tendría que hablar?", 2),
        turn("customer", "Mi vecino quiere vender su casa", 3),
        turn("customer", "Hola", 4),
    ]
    ledger, active, count = update(messages)
    assert ledger == []
    assert active is None and count == 0


def test_unsupported_model_visit_never_creates_an_opportunity():
    message = turn("customer", "Busco arriendo en Maipú", 1)
    summary = classify_hybrid(
        [message], llm_enabled=True,
        propose=lambda *_: {
            "intent": "ASK_VISIT", "confidence": .99,
            "needs_review": False, "search": {},
        },
    )
    ledger, _, _ = update([message], summary=summary)
    assert all(o.get("case_type") != "VISITA_SOLICITADA" for o in ledger)


def test_late_replay_does_not_reopen_a_closed_case():
    visit = turn("customer", "Quiero agendar una visita", 1)
    cancel = turn("customer", "Cancela la visita", 2)
    ledger, _, _ = update([visit, cancel])
    assert ledger[0]["status"] == "DESCARTADA"
    later, active, count = update([visit], ledger)
    assert later == ledger
    assert active is None and count == 0


def test_original_ledger_is_not_mutated():
    message = turn("customer", "Quiero agendar una visita", 1)
    ledger, _, _ = update([message])
    snapshot = deepcopy(ledger)
    update([turn("human_agent", "Perfecto, lo reviso", 2)], ledger)
    assert ledger == snapshot
