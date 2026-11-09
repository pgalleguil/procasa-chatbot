"""End-to-end safety simulation through V2 attribution and durable queue."""

from scripts.run_price_drop_classifier_acceptance import run_acceptance_matrix


def test_forty_multi_turn_conversations_pass_in_isolated_mocks():
    result = run_acceptance_matrix()
    assert result["execution"]["conversation_count"] == 40
    assert result["execution"]["turn_count"] >= 120
    assert result["execution"]["successive_drop_conversations"] >= 10
    assert result["execution"]["live_generator_used"] is False
    assert result["execution"]["production_effects"] is False
    assert result["summary"]["acceptance_status"] == "PASS"
    assert result["summary"]["passed_conversations"] == 40
    assert result["summary"]["failed_turns"] == 0
