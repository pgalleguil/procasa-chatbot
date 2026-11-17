"""Real MongoDB integration. Opt in ONLY with a loopback, disposable server.

EXEC_AUDIT_TEST_MONGO_URI=mongodb://127.0.0.1:27931
No mocks for atomic claims, projections, CAS, or aggregation expressions.
"""
import json
import os
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from bson import ObjectId
from pymongo import MongoClient
from pymongo.uri_parser import parse_uri

from chatbot import executive_intelligence_worker as worker
from chatbot import executive_whatsapp_audit as audit

OWNER = ObjectId('000000000000000000000001')
NOW = datetime.now(timezone.utc)


@pytest.fixture
def db(monkeypatch):
    uri = os.getenv('EXEC_AUDIT_TEST_MONGO_URI')
    if not uri:
        pytest.skip('requires explicitly configured disposable local MongoDB')
    parsed = parse_uri(uri)
    assert all(host in {'127.0.0.1', 'localhost', '::1'} for host, _ in parsed['nodelist'])
    assert not parsed.get('username') and not parsed.get('password')
    client = MongoClient(uri, serverSelectionTimeoutMS=2000, tz_aware=True)
    client.admin.command('ping')
    name = 'executive_audit_test_' + uuid4().hex
    database = client[name]
    database.usuarios.insert_one({'_id': OWNER, 'nombre': 'Synthetic Agent', 'rol': 'agente',
                                 'is_active': True, 'wasender': {'session_id': 100}})
    monkeypatch.setenv('PROCASA_WASENDER_SYNTHETIC_AGENT', json.dumps({
        'session_id': 100, 'webhook_secret': 'synthetic-secret-only',
        'api_token': 'synthetic-token-only', 'capture_enabled': True}))
    yield database
    # Only this newly created, uniquely named database on a loopback server.
    assert name.startswith('executive_audit_test_') and len(name) == 53
    client.drop_database(name)
    client.close()


def seed(db, count=2):
    lead = {'_id': ObjectId(), 'phone': '+56911112222',
            'lifecycle': {'assigned_to_user_id': str(OWNER), 'current_assignment_cycle_id': 'cycle-A'},
            'messages': [], 'commercial_intelligence': {
                'pending': True, 'pending_message_id': str(count - 1), 'pending_cycle_id': 'cycle-A',
                'pending_session_id': '100', 'pending_source': 'executive_whatsapp', 'pending_at': NOW}}
    for i in range(count):
        lead['messages'].append({'message_id': str(i), 'provider_message_id': str(i),
            'timestamp': (NOW + timedelta(seconds=i)).isoformat(), 'timestamp_source': 'provider',
            'channel': 'executive_whatsapp', 'assignment_cycle_id': 'cycle-A',
            'assigned_executive_id': str(OWNER), 'wasender_session_id': '100',
            'actor_type': 'customer' if i % 2 else 'human_agent',
            'content': 'Busco casa hasta 3500 UF' if i == 1 else 'Hola'})
    db.leads.insert_one(lead)
    db.crm_assignment_cycles.insert_one({'lead_id': str(lead['_id']), 'assignment_cycle_id': 'cycle-A',
        'assigned_to_user_id': str(OWNER), 'cycle_status': 'active', 'unassigned_at': None,
        'assigned_at': NOW - timedelta(hours=1)})
    return lead


def test_atomic_claim_only_one_worker_and_no_history_transfer(db):
    lead = seed(db)
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda _: worker._claim(db, lead['_id'], now=NOW), range(6)))
    winners = [c for c in claims if c]
    assert len(winners) == 1
    assert 'messages' not in winners[0]


def test_real_pipeline_incremental_batches_preserve_first_need(db):
    lead = seed(db, count=450)
    for number in range(3):
        claimed = worker._claim(db, lead['_id'], now=NOW + timedelta(minutes=number))
        assert worker._load_window(db, claimed)
        assert len(claimed['messages']) <= worker.WINDOW + 2
        assert worker.process_claimed(db, claimed, llm_enabled=False) == 'ready'
        saved = db.leads.find_one({'_id': lead['_id']})
        assert saved['commercial_intelligence']['summary']['search']['max_price_uf'] == 3500
        assert saved['commercial_intelligence']['pending'] is (number < 2)
    assert saved['commercial_intelligence']['last_processed_message_id'] == '449'


def test_new_message_during_classification_cannot_be_cleared(db):
    lead = seed(db)
    claimed = worker._claim(db, lead['_id'], now=NOW)
    def model(*_):
        db.leads.update_one({'_id': lead['_id']}, {'$set': {'commercial_intelligence.pending_message_id': 'new'}})
        return dict(intent='SEARCH_UPDATED', confidence=.5, search={}, needs_review=True)
    assert worker.process_claimed(db, claimed, propose=model) == 'superseded'
    saved = db.leads.find_one({'_id': lead['_id']})
    assert saved['commercial_intelligence']['pending']
    assert 'summary' not in saved['commercial_intelligence']


def test_expired_worker_cannot_finish_new_lease(db):
    lead = seed(db)
    old = worker._claim(db, lead['_id'], now=NOW)
    new = worker._claim(db, lead['_id'], now=NOW + timedelta(seconds=91))
    assert old['commercial_intelligence']['lease_token'] != new['commercial_intelligence']['lease_token']
    assert worker._finish(db, old, summary={'intent': 'OPT_OUT'}, status='ready') == 'superseded'
    assert db.leads.find_one({'_id': lead['_id']})['commercial_intelligence']['lease_token'] == new['commercial_intelligence']['lease_token']


@pytest.mark.parametrize('mutation', ['owner', 'cycle', 'disabled', 'capture'])
def test_revalidation_after_model_rejects_revocation(db, monkeypatch, mutation):
    lead = seed(db)
    claimed = worker._claim(db, lead['_id'], now=NOW)
    def model(*_):
        if mutation == 'owner':
            db.leads.update_one({'_id': lead['_id']}, {'$set': {'lifecycle.assigned_to_user_id': 'other'}})
        elif mutation == 'cycle':
            db.crm_assignment_cycles.update_one({'assignment_cycle_id': 'cycle-A'}, {'$set': {'cycle_status': 'closed'}})
        elif mutation == 'disabled':
            db.usuarios.update_one({'_id': OWNER}, {'$set': {'is_active': False}})
        else:
            monkeypatch.setenv('PROCASA_WASENDER_SYNTHETIC_AGENT', '{}')
        return dict(intent='SEARCH_UPDATED', search={}, confidence=.5, needs_review=False)
    assert worker.process_claimed(db, claimed, propose=model) in ('stale_assignment', 'superseded')
    assert 'summary' not in db.leads.find_one({'_id': lead['_id']})['commercial_intelligence']


def test_old_cycle_and_private_channel_never_leave_mongo(db):
    lead = seed(db)
    private = dict(lead['messages'][0], content='private-sentinel', message_id='private', channel='office')
    old = dict(private, channel='executive_whatsapp', assignment_cycle_id='old-cycle', message_id='old')
    db.leads.update_one({'_id': lead['_id']}, {'$push': {'messages': {'$each': [private, old]}}})
    claimed = worker._claim(db, lead['_id'], now=NOW)
    assert worker._load_window(db, claimed)
    assert all(m['content'] != 'private-sentinel' for m in claimed['messages'])


def test_duplicate_append_is_atomic_and_no_crm_side_effect(db):
    lead = seed(db)
    cycle = db.crm_assignment_cycles.find_one({'assignment_cycle_id': 'cycle-A'})
    parsed = dict(timestamp=NOW.isoformat(), timestamp_source='provider',
                  provider_message_id='new-event', from_me=False, content='Quiero agendar una visita')
    session = {'user_id': str(OWNER), 'session_id': '100'}
    def append(_):
        return audit._record_authorized(db, session=session, phone=lead['phone'], parsed=parsed, lead=lead, cycle=cycle)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(append, range(6)))
    assert results.count('stored') == 1
    assert len(db.leads.find_one({'_id': lead['_id']})['messages']) == 3
    assert db.crm_assignment_cycles.find_one({'_id': cycle['_id']}) == cycle
    assert db.crm_events.count_documents({}) == 0


def test_leased_front_rows_do_not_starve_other_leads(db):
    for _ in range(20):
        lead = seed(db)
        worker._claim(db, lead['_id'], now=NOW)
    eligible = seed(db)
    result = worker.process_pending_batch(db, limit=1, llm_enabled=False)
    assert result['processed'] == 1
    assert db.leads.find_one({'_id': eligible['_id']})['commercial_intelligence']['pending'] is False


def test_checkpoint_loss_is_visible(db):
    lead = seed(db)
    db.leads.update_one({'_id': lead['_id']}, {'$set': {
        'commercial_intelligence.version': worker.VERSION,
        'commercial_intelligence.last_processed_cycle_id': 'cycle-A',
        'commercial_intelligence.last_processed_message_id': 'missing'}})
    claimed = worker._claim(db, lead['_id'], now=NOW)
    assert worker.process_claimed(db, claimed, llm_enabled=False) == 'ready'
    summary = db.leads.find_one({'_id': lead['_id']})['commercial_intelligence']['summary']
    assert summary['history_incomplete'] and summary['needs_review']


def test_v1_search_is_rebuilt_not_blindly_trusted(db):
    lead = seed(db)
    db.leads.update_one({'_id': lead['_id']}, {'$set': {
        'commercial_intelligence.version': 'executive_hybrid_v1',
        'commercial_intelligence.last_processed_cycle_id': 'cycle-A',
        'commercial_intelligence.last_processed_message_id': '1',
        'commercial_intelligence.summary': {'intent': 'OPT_OUT', 'search': {'max_price_uf': 99999}}}})
    claimed = worker._claim(db, lead['_id'], now=NOW)
    assert worker.process_claimed(db, claimed, llm_enabled=False) == 'ready'
    summary = db.leads.find_one({'_id': lead['_id']})['commercial_intelligence']['summary']
    assert summary['search']['max_price_uf'] == 3500
    assert summary['contact_permission'] == 'OPTED_OUT'


def test_pending_index_is_idempotent(db):
    worker.ensure_indexes(db)
    worker.ensure_indexes(db)
    index = db.leads.index_information()['executive_intelligence_pending_lease']
    assert index['partialFilterExpression'] == {'commercial_intelligence.pending': True}


def test_office_append_preserves_executive_evidence(db, monkeypatch):
    from chatbot import storage, conversation_observability, crm_updates
    lead = seed(db, 120)
    monkeypatch.setattr(storage, 'get_db', lambda: db)
    monkeypatch.setattr(conversation_observability, 'resolve_or_create_conversation', lambda *a, **k: 'synthetic-conversation')
    monkeypatch.setattr(crm_updates, 'bump_crm_leads_version', lambda *a, **k: None)
    storage.guardar_mensaje(lead['phone'], 'assistant', 'Respuesta de oficina', lead_id=str(lead['_id']))
    saved = db.leads.find_one({'_id': lead['_id']})
    assert len(saved['messages']) == 121
    assert saved['messages'][:120] == lead['messages']
    history = storage.obtener_conversacion(lead['phone'])
    assert len(history) == 1 and history[0]['content'] == 'Respuesta de oficina'


def test_real_mongo_global_optout_is_durable_and_campaign_safe(db):
    from chatbot.price_drop_campaign_classifier import lead_must_not_be_contacted
    from chatbot.price_drop_leads_v2 import _contact_suppression_reasons
    lead = seed(db)
    cycle = db.crm_assignment_cycles.find_one({'assignment_cycle_id': 'cycle-A'})
    session = {'user_id': str(OWNER), 'session_id': '100'}
    parsed = dict(timestamp=NOW.isoformat(), timestamp_source='provider',
                  provider_message_id='client-global-stop', from_me=False,
                  content='No quiero más avisos')
    assert audit._record_authorized(
        db, session=session, phone=lead['phone'], parsed=parsed,
        lead=lead, cycle=cycle) == 'stored'
    saved = db.leads.find_one({'_id': lead['_id']})
    assert saved['price_drop_v2']['opted_out'] is True
    assert saved['price_drop_campaign_v1']['opted_out'] is True
    assert saved['contact_restrictions']['marketing_opt_out'] is True
    assert saved['contact_restrictions']['marketing_opt_out_provider_id'] == 'client-global-stop'
    assert lead_must_not_be_contacted(saved)
    assert 'explicit_opt_out' in _contact_suppression_reasons(saved)
    assert audit._record_authorized(
        db, session=session, phone=lead['phone'], parsed=parsed,
        lead=lead, cycle=cycle) == 'duplicate_or_reassigned'
    assert len(db.leads.find_one({'_id': lead['_id']})['messages']) == 3
    # A later customer-initiated service request is still a valid message;
    # it must never undo the marketing restriction.
    parsed = dict(parsed, provider_message_id='new-service',
                  content='Ahora necesito arrendar un departamento')
    assert audit._record_authorized(
        db, session=session, phone=lead['phone'], parsed=parsed,
        lead=lead, cycle=cycle) == 'stored'
    after = db.leads.find_one({'_id': lead['_id']})
    assert after['contact_restrictions']['marketing_opt_out'] is True
    assert lead_must_not_be_contacted(after)
    assert after.get('conversation_status') is None
