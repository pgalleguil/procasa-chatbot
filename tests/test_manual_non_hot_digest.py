from pathlib import Path

def test_manual_lead_persists_verified_commercial_source_and_accumulates_digest():
    source = Path("chatbot/manual_entry.py").read_text(encoding="utf-8")
    assert '"source_event_type": "MANUAL_LEAD_CREATED"' in source
    assert "accumulate_non_hot_lead(db, lead=fresh_lead, cycle=cycle)" in source

def test_non_hot_digest_uses_fixed_window_and_due_claim():
    source = Path("chatbot/crm_non_hot_digest.py").read_text(encoding="utf-8")
    assert 'CRM_NON_HOT_DIGEST_WINDOW_MINUTES' in source
    assert '"send_after": {"$lte": current}' in source


def test_digest_window_is_anchored_to_assignment_not_worker_recovery_time():
    source = Path("chatbot/crm_non_hot_digest.py").read_text(encoding="utf-8")
    assert 'now = coerce_utc_datetime(db_cycle.get("assigned_at")) or utc_now()' in source


def test_recovery_claim_can_be_narrowed_to_exact_digest():
    source = Path("chatbot/crm_non_hot_digest.py").read_text(encoding="utf-8")
    assert "notification_id=None" in source
    assert 'extra_filter["_id"] = notification_id' in source


def test_retryable_digest_respects_retry_after_and_releases_cycle_reservation():
    notifications = Path("chatbot/crm_notifications.py").read_text(encoding="utf-8")
    digest = Path("chatbot/crm_non_hot_digest.py").read_text(encoding="utf-8")
    assert '"next_attempt_at": {"$lte": now}' in notifications
    assert 'def release_cycle_delivery' in notifications
    assert 'release_cycle_delivery(' in digest
    assert 'http_status == 429' in digest


def test_retryable_digest_accepts_null_provider_id_but_not_accepted_delivery():
    source = Path("chatbot/crm_non_hot_digest.py").read_text(encoding="utf-8")
    assert '"provider_message_id": {"$in": [None]}' in source
    assert '"actually_delivered": {"$ne": True}' in source
