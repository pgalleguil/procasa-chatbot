from crm_schema import normalize_classification


def test_rule_state_is_not_overwritten_by_deepseek_final_state():
    raw = {
        "state": "INCIERTO", "rule_state": "INCONCLUSIVE", "source": "deepseek",
        "deepseek_status": "VALID", "deepseek_raw": {"choices": [{"message": {"content": "{}"}}]},
        "deepseek_payload": {"messages": []}, "deepseek_message_content": "{}",
        "deepseek_reasoning_content": "r", "analysis_at": "2026-07-14T00:00:00Z",
    }
    result = normalize_classification(raw)
    assert result["rule_state"] == "INCONCLUSIVE"
    assert result["final_state"] == "INCIERTO"
    assert result["deepseek_payload"] == {"messages": []}
    assert result["deepseek_message_content"] == "{}"
    assert result["assignment_ready"] is False


def test_non_owner_states_are_never_assignment_ready():
    for state in ("INCIERTO", "CORREDOR_SEGURO", "CORREDOR_PROBABLE", "AD_REMOVED"):
        result = normalize_classification({
            "state": state,
            "source": "deepseek",
            "deepseek_status": "VALID",
            "deepseek_raw": {"choices": [{}]},
            "assignment_ready": True,
        })
        assert result["assignment_ready"] is False, state


def test_valid_deepseek_owner_state_is_assignment_ready():
    raw = {"state": "DUEÑO_SEGURO", "rule_state": "INCONCLUSIVE", "source": "deepseek",
           "deepseek_status": "VALID", "deepseek_raw": {"choices": [{}]}}
    assert normalize_classification(raw)["assignment_ready"] is True
