from chatbot.crm_metrics import event_evidence


def test_legacy_owner_detail_results_are_valid_sla_stops():
    for raw_result, expected, effective in [
        ("owner_otro", "OTHER_EXPLICIT", True),
        ("no_responde_llamada", "CALL_NO_ANSWER", False),
    ]:
        result = {
            "type": "HUMAN_NOTE", "lead_id": "a", "actor": "agent",
            "confirmed": True, "result": raw_result,
        }
        evidence = event_evidence(result)
        assert evidence["management"] is True
        assert evidence["result"] == expected
        assert evidence["effective_contact"] is effective
