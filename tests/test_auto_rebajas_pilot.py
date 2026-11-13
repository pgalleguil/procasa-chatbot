"""Guard rails for automatic (no per-recipient approval) price-drop batches."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import scripts.enviar_rebajas_automatico as automatic


def test_historical_inquiry_eligible_without_separate_optin_but_stop_excludes(monkeypatch):
    event = SimpleNamespace(event_id="price_event_123", dropped_at=datetime.now(timezone.utc))
    candidate = {"eligible": True, "lead_id": "a" * 24}
    lead = {"_id": candidate["lead_id"], "phone": "+56911111111"}
    class Collection:
        def find_one(self, query):
            return lead
    db = {"leads": Collection()}
    monkeypatch.setattr(automatic.cli, "_events", lambda *args, **kwargs: [event])
    monkeypatch.setattr(automatic.cli, "_lead_id", lambda value: value)
    monkeypatch.setattr(automatic, "find_candidates_for_event",
                        lambda *args, **kwargs: [candidate])
    monkeypatch.setattr(automatic, "lead_must_not_be_contacted", lambda lead: False)
    rows, _ = automatic.choose_new_recipients(db, max_messages=3, max_age_days=120,
                                               now=datetime.now(timezone.utc))
    assert len(rows) == 1
    monkeypatch.setattr(automatic, "lead_must_not_be_contacted", lambda lead: True)
    rows, _ = automatic.choose_new_recipients(db, max_messages=3, max_age_days=120,
                                               now=datetime.now(timezone.utc))
    assert rows == []


def test_recent_sent_campaign_blocks_repeated_outreach():
    now = datetime.now(timezone.utc)
    lead = {"price_drop_v2": {"campaigns": [
        {"status": "SENT", "sent_at": now - timedelta(days=1)},
    ]}}
    assert automatic.recent_outreach_or_pending(lead, now=now)
    lead["price_drop_v2"]["campaigns"][0]["sent_at"] = now - timedelta(days=35)
    assert not automatic.recent_outreach_or_pending(lead, now=now)
    lead["price_drop_v2"]["campaigns"][0]["status"] = "DELIVERY_UNKNOWN"
    assert automatic.recent_outreach_or_pending(lead, now=now)


def test_execution_never_sends_partial_three_lead_pilot(monkeypatch):
    event = SimpleNamespace(event_id="evt_1", property_code="6436")
    monkeypatch.setattr(automatic, "choose_new_recipients",
                        lambda *args, **kwargs: ([{"event": event, "lead_id": "a" * 24, "last4": "1111"}],
                                                  {"events_examined": 1, "events_discovered": 1}))
    monkeypatch.setattr(automatic, "verify_publications",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("must not perform live verification or writes")))
    outcome = automatic.run({}, execute=True, max_messages=3)
    assert outcome["status"] == "NOT_ENOUGH_ELIGIBLE_RECIPIENTS"
    assert outcome["attempts"] == 0


def test_automatic_batch_calls_existing_approved_sender_without_per_lead_prompt(monkeypatch):
    event = SimpleNamespace(event_id="evt_1", campaign_id="evt_1", property_code="6436")
    row = {"event": event, "lead_id": "a" * 24, "last4": "1111"}
    monkeypatch.setattr(automatic, "choose_new_recipients",
                        lambda *args, **kwargs: ([row],
                                                  {"events_examined": 1, "events_discovered": 1}))
    monkeypatch.setattr(automatic, "verify_publications",
                        lambda *args, **kwargs: {"evt_1": {"sendable": True}})
    monkeypatch.setattr(automatic.cli, "_lead_id", lambda value: value)
    monkeypatch.setattr(automatic.cli, "_backup_leads", lambda *args: "/tmp/mock_backup")
    operations = []
    monkeypatch.setattr(automatic.cli, "prepare_selected",
                        lambda *args, **kwargs: operations.append("prepare"))
    monkeypatch.setattr(automatic.v2, "approve_one",
                        lambda *args, **kwargs: operations.append("approve"))
    def sender(*args, **kwargs):
        assert kwargs["already_confirmed"] is True
        assert kwargs["max_messages"] == 1
        operations.append("sender")
        return {"status": "COMPLETED", "attempts": 1}
    monkeypatch.setattr(automatic.cli, "execute_approved_batch", sender)
    monkeypatch.setenv(automatic.v2.SEND_FLAG, "1")
    monkeypatch.setenv(automatic.v2.CONTEXT_FLAG, "1")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    outcome = automatic.run({}, execute=True, max_messages=1)
    assert outcome["attempts"] == 1
    assert operations == ["prepare", "approve", "sender"]

def test_original_local_cli_routes_new_batch_without_touching_old_pilot(monkeypatch, capsys):
    import scripts.enviar_piloto_rebajas as cli
    calls = []
    monkeypatch.setattr(cli, "_get_db", lambda: object())
    monkeypatch.setattr(automatic, "run", lambda db, **kwargs: (
        calls.append(kwargs) or {"status": "COMPLETED", "attempts": 3}))
    result = cli.main(["--auto", "--execute", "--max-messages", "3"])
    assert result == 0
    assert len(calls) == 1
    assert calls[0]["execute"] is True
    assert calls[0]["max_messages"] == 3
    assert "COMPLETED" in capsys.readouterr().out


def test_original_local_cli_auto_preview_does_not_send(monkeypatch):
    import scripts.enviar_piloto_rebajas as cli
    monkeypatch.setattr(cli, "_get_db", lambda: object())
    called = []
    monkeypatch.setattr(automatic, "run", lambda db, **kwargs: (
        called.append(kwargs) or {"status": "CANDIDATES_SELECTED", "attempts": 0}))
    assert cli.main(["--auto", "--dry-run", "--max-messages", "3"]) == 0
    assert called[0]["execute"] is False


def test_original_local_cli_rejects_auto_with_original_three_pilot(monkeypatch):
    import scripts.enviar_piloto_rebajas as cli
    monkeypatch.setattr(cli, "_get_db", lambda: (_ for _ in ()).throw(
        AssertionError("must be rejected before DB or sender use")))
    assert cli.main(["--auto", "--pilot", "--execute", "--max-messages", "3"]) == 2
