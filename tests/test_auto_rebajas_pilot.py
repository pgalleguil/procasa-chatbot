"""Guard rails for automatic (no per-recipient approval) price-drop batches."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import scripts.enviar_rebajas_automatico as automatic


def test_historical_inquiry_eligible_without_separate_optin_but_stop_excludes(monkeypatch):
    event = SimpleNamespace(event_id="price_event_123", dropped_at=datetime.now(timezone.utc))
    candidate = {"eligible": True, "lead_id": "a" * 24}
    lead = {"_id": candidate["lead_id"], "phone": "+56911111111"}
    class FakeCursor(list):
        def batch_size(self, size):
            return self

    class Collection:
        def find_one(self, query):
            return lead

        def find(self, query, projection=None):
            return FakeCursor([lead])

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


def test_execution_sends_available_recipients_below_operator_limit(monkeypatch):
    event = SimpleNamespace(event_id="evt_1", campaign_id="evt_1",
                            property_code="6436")
    recipient = {"event": event, "lead_id": "a" * 24, "last4": "1111"}
    monkeypatch.setattr(automatic, "choose_new_recipients",
                        lambda *args, **kwargs: ([recipient],
                                                  {"events_examined": 1, "events_discovered": 1}))
    monkeypatch.setattr(automatic, "verify_publications",
                        lambda *args, **kwargs: {"evt_1": {"sendable": True}})
    monkeypatch.setattr(automatic.cli, "_lead_id", lambda v: v)
    monkeypatch.setattr(automatic.cli, "_backup_leads",
                        lambda *args: "/tmp/mock_backup")
    operations = []
    monkeypatch.setattr(automatic.cli, "prepare_selected",
                        lambda *args, **kwargs: operations.append("prepare"))
    monkeypatch.setattr(automatic.v2, "approve_one",
                        lambda *args, **kwargs: operations.append("approve"))
    def fake_dispatch(*args, **kwargs):
        assert kwargs["max_messages"] == 20
        assert kwargs["daily_provider_cap"] == 20
        operations.append("send")
        return {"status": "COMPLETED", "attempts": 1}
    monkeypatch.setattr(automatic.cli, "execute_approved_batch", fake_dispatch)
    monkeypatch.setenv(automatic.v2.SEND_FLAG, "1")
    monkeypatch.setenv(automatic.v2.CONTEXT_FLAG, "1")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    result = automatic.run({}, execute=True, max_messages=20,
                           daily_provider_cap=20)
    assert result["status"] == "COMPLETED"
    assert result["selected"] == 1
    assert result["attempts"] == 1
    assert operations == ["prepare", "approve", "send"]


def test_zero_recipients_is_a_clean_noop(monkeypatch):
    monkeypatch.setattr(automatic, "choose_new_recipients",
                        lambda *args, **kwargs: ([], {"events_examined": 3,
                                                        "events_discovered": 3}))
    monkeypatch.setattr(automatic, "verify_publications",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("no publication checks when there are no leads")))
    result = automatic.run({}, execute=True, max_messages=20)
    assert result["status"] == "NO_ELIGIBLE_RECIPIENTS"
    assert result["attempts"] == 0


def test_full_history_is_default_and_no_hidden_event_limit(monkeypatch):
    inspected = []
    def events(*args, **kwargs):
        inspected.append(kwargs.get("limit"))
        return []
    monkeypatch.setattr(automatic.cli, "_events", events)
    selected, _ = automatic.choose_new_recipients(
        {}, max_messages=20, max_age_days=0,
        now=datetime.now(timezone.utc))
    assert selected == []
    assert inspected == [None]


def test_unpublished_recipient_does_not_hold_up_other_campaigns(monkeypatch):
    good = SimpleNamespace(event_id="good", campaign_id="good", property_code="6436")
    stale = SimpleNamespace(event_id="stale", campaign_id="stale", property_code="6377")
    rows = [{"event": good, "lead_id": "a" * 24, "last4": "1111"},
            {"event": stale, "lead_id": "b" * 24, "last4": "2222"}]
    monkeypatch.setattr(automatic, "choose_new_recipients",
                        lambda *args, **kwargs: (rows,
                                                  {"events_examined": 2, "events_discovered": 2}))
    monkeypatch.setattr(automatic, "verify_publications",
                        lambda *args, **kwargs: {"good": {"sendable": True},
                                                "stale": {"sendable": False,
                                                          "status": "inactive"}})
    monkeypatch.setattr(automatic.cli, "_lead_id", lambda v: v)
    monkeypatch.setattr(automatic.cli, "_backup_leads",
                        lambda *args: "/tmp/mock_backup")
    prepared = []
    monkeypatch.setattr(automatic.cli, "prepare_selected",
                        lambda *args, **kwargs: prepared.append(kwargs["event"].event_id))
    monkeypatch.setattr(automatic.v2, "approve_one", lambda *args, **kwargs: None)
    monkeypatch.setattr(automatic.cli, "execute_approved_batch",
                        lambda *args, **kwargs: {"status": "COMPLETED", "attempts": 1})
    monkeypatch.setenv(automatic.v2.SEND_FLAG, "1")
    monkeypatch.setenv(automatic.v2.CONTEXT_FLAG, "1")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    result = automatic.run({}, execute=True, max_messages=20)
    assert result["attempts"] == 1
    assert prepared == ["good"]
    assert len(result["publication_exclusions"]) == 1
    assert result["publication_exclusions"][0]["code"] == "6377"


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
