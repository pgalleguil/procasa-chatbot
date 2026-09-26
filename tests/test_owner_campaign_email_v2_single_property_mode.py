from __future__ import annotations

from analytics import owner_campaign_email_v2 as renderer


def test_renderer_selects_frozen_single_property_copy_and_macro_context(monkeypatch):
    captured = {}

    class FakeTemplate:
        def render(self, **context):
            captured.update(context)
            return "rendered"

    class FakeEnvironment:
        def __init__(self, **_kwargs):
            pass

        def get_template(self, _name):
            return FakeTemplate()

    monkeypatch.setattr(renderer, "Environment", FakeEnvironment)
    monkeypatch.setattr(renderer, "_valuation_slots", lambda _model: [])
    monkeypatch.setattr(renderer, "_portfolio_summary", lambda _model: {})
    monkeypatch.setattr(renderer, "_initials", lambda _name: "EG")

    result = renderer.render_owner_campaign_email_v2(
        [{"code": "5641", "is_rental": False}],
        email="qa@example.test",
        executives=[],
        base_url="https://example.test",
    )

    assert result == "rendered"
    assert captured["single_property_only"] is True
    assert captured["hero_title"] == "Revisión comercial de tu propiedad"
    assert "Analizamos el comportamiento reciente" in captured["hero_description"]
    assert len(captured["macro_context"]["copy_paragraphs"]) == 2
    assert "Banco Central de Chile" in captured["macro_context"]["source_line"]
    assert captured["report_date"]


def test_renderer_keeps_legacy_layout_for_multiproperty_and_rental(monkeypatch):
    captured = {}

    class FakeTemplate:
        def render(self, **context):
            captured.update(context)
            return "rendered"

    class FakeEnvironment:
        def __init__(self, **_kwargs):
            pass

        def get_template(self, _name):
            return FakeTemplate()

    monkeypatch.setattr(renderer, "Environment", FakeEnvironment)
    monkeypatch.setattr(renderer, "_valuation_slots", lambda _model: [])
    monkeypatch.setattr(renderer, "_portfolio_summary", lambda _model: {})
    monkeypatch.setattr(renderer, "_initials", lambda _name: "EG")

    renderer.render_owner_campaign_email_v2(
        [{"code": "5641", "is_rental": False}, {"code": "9000", "is_rental": False}],
        email="qa@example.test",
        executives=[],
        base_url="https://example.test",
    )
    assert captured["single_property_only"] is False
    assert captured["macro_context"]["copy_paragraphs"] == ()

    renderer.render_owner_campaign_email_v2(
        [{"code": "5641", "is_rental": True}],
        email="qa@example.test",
        executives=[],
        base_url="https://example.test",
    )
    assert captured["single_property_only"] is False
    assert captured["hero_title"].endswith("arriendo")
