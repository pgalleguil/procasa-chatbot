"""Compatibility checks for retired V1 price-drop send entry points.

New campaigns are prepared, approved, sent, and resumed only through the
shared V2 CLI. These tests make sure obsolete V1 entry points fail closed.
The V2 campaign, inbound, notification, and idempotency behavior is covered
by test_price_drop_leads_v2.py and test_price_drop_campaign_cli.py.
"""
from __future__ import annotations

import json

import mongomock

from chatbot.price_drop_campaign_bootstrap import run_bootstrap
from chatbot.price_drop_campaign_real import send_one_due
from scripts.price_drop_campaign_real_pilot import main as legacy_main


class NoDatabaseAccess:
    def __getitem__(self, _collection):
        raise AssertionError("retired V1 entry point must not access MongoDB")


def test_legacy_v1_sender_is_closed_without_provider_call_or_database_write():
    db = mongomock.MongoClient()["URLS"]
    provider_calls = []

    result = send_one_due(
        db, campaign_id="legacy-campaign", sender=lambda *args: provider_calls.append(args),
    )

    assert result == {
        "status": "BLOCKED",
        "reason": "legacy_v1_sender_removed_use_shared_campaign_cli",
    }
    assert provider_calls == []
    assert db.list_collection_names() == []


def test_legacy_render_bootstrap_is_closed_without_database_access():
    result = run_bootstrap(NoDatabaseAccess())

    assert result["status"] == "BLOCKED"
    assert result["writes"] == 0
    assert result["sends"] == 0


def test_legacy_campaign_cli_is_closed_and_directs_to_shared_cli(capsys):
    assert legacy_main([]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "BLOCKED"
    assert output["reason"] == "legacy_campaign_sender_removed_use_scripts_enviar_piloto_rebajas"
