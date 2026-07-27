from pathlib import Path

def test_manual_lead_persists_verified_commercial_source_and_accumulates_digest():
    source = Path("chatbot/manual_entry.py").read_text(encoding="utf-8")
    assert '"source_event_type": "MANUAL_LEAD_CREATED"' in source
    assert "accumulate_non_hot_lead(db, lead=fresh_lead, cycle=cycle)" in source

def test_non_hot_digest_uses_fixed_window_and_due_claim():
    source = Path("chatbot/crm_non_hot_digest.py").read_text(encoding="utf-8")
    assert 'CRM_NON_HOT_DIGEST_WINDOW_MINUTES' in source
    assert '"send_after": {"$lte": current}' in source
