"""DeepSeek JSON transport contract. All responses are local fakes."""
from types import SimpleNamespace

import pytest

from chatbot.executive_whatsapp_deepseek import make_deepseek_audit_transport


class StubCompletions:
    def __init__(self, *, body='{"commercial_status":"UNKNOWN"}', finish="stop"):
        self.calls = []
        self.body, self.finish = body, finish

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[
            SimpleNamespace(
                finish_reason=self.finish,
                message=SimpleNamespace(content=self.body),
            ),
        ])


class StubClient:
    def __init__(self, **kwargs):
        self.chat = SimpleNamespace(completions=StubCompletions(**kwargs))


def sample_request():
    return {
        "task": "commercial_audit_candidate_only",
        "no_automatic_actions": True,
        "system": "Devuelve JSON con evidencia; no actúes.",
        "coverage_complete": False,
        "messages": [
            {"actor_type": "client", "content": "Busco departamento"},
        ],
        "required_schema": {"commercial_status": ["UNKNOWN"]},
    }


def transport(client=None, **kwargs):
    return make_deepseek_audit_transport(
        api_key="fake-api-token-not-real",
        privacy_authorization="EXPLICITLY_APPROVED_DATA_PROCESSING",
        client=client or StubClient(),
        **kwargs,
    )


def test_privacy_gate_rejects_before_creating_client_or_network():
    with pytest.raises(PermissionError, match="privacy_review_required"):
        make_deepseek_audit_transport(
            api_key="fake-api-token-not-real",
            privacy_authorization="",
            client=StubClient(),
        )


def test_json_response_uses_current_model_and_no_tool_calls():
    fake = StubClient()
    output = transport(fake)(sample_request())
    assert output == '{"commercial_status":"UNKNOWN"}'
    call = fake.chat.completions.calls[0]
    assert call["model"] == "deepseek-flash"
    assert call["thinking"] == {"type": "disabled"}
    assert call["response_format"] == {"type": "json_object"}
    assert call["max_tokens"] <= 900
    assert "tools" not in call
    assert "recipient" not in str(call)


def test_truncated_model_output_fails_closed():
    fake = StubClient(finish="length")
    with pytest.raises(ValueError, match="incomplete_completion"):
        transport(fake)(sample_request())


def test_empty_response_fails_closed():
    fake = StubClient(body="   ")
    with pytest.raises(ValueError, match="empty_completion"):
        transport(fake)(sample_request())


@pytest.mark.parametrize("update", [
    {"task": "send_whatsapp"},
    {"no_automatic_actions": False},
    {"messages": []},
    {"system": "Answer normally."},
])
def test_no_unauthorized_model_request(update):
    value = sample_request()
    value.update(update)
    with pytest.raises(ValueError):
        transport()(value)


def test_model_name_is_fixed_allowlist():
    with pytest.raises(ValueError, match="unsupported_deepseek_audit_model"):
        transport(model="third-party-model")
