"""Tests for CRM SLA Alert Evaluator — Phase 1, full coverage.

79 tests covering: templates (uniform text, 8 variants, no duplicates),
outreach classification (CALL_RESULT excluded, MWR→whatsapp_sent),
cutover, management policy, async phone, exclusions, business minutes, UTF-8, E2E.
"""
from datetime import datetime, timedelta, timezone

import pytest

from chatbot.crm_sla_alert_templates import (
    build_sla_message, build_lead_url, build_deadline_display,
    outreach_channel_label, MESSAGE_DOMAIN,
    EXPLAIN, ACTION,
)
from chatbot.crm_sla_alert_evaluator import (
    classify_outreach_state, add_business_minutes,
    _lead_first_name, _lead_property_code, _is_test_lead,
    evaluate_sla_alerts, _get_executive_phone_async,
    SLA_STOP_RESULTS, OUTREACH_RESULTS,
    THRESHOLD_WARNING_NORMAL, THRESHOLD_BREACHED_NORMAL,
    THRESHOLD_WARNING_HOT, THRESHOLD_BREACHED_HOT,
    ALERT_LEVEL_WARNING, ALERT_LEVEL_BREACHED,
    SLA_PROFILE_STANDARD, SLA_PROFILE_HOT,
)
from chatbot.constants import CHILE_TZ


def _msg(**kw):
    defaults = dict(
        client_first_name="Ana", property_code="P123",
        elapsed_minutes=160, deadline_display="24/07/2026 12:00",
        lead_url="https://crm.example.com/crm/lead-id/abc", outreach_state="none",
    )
    defaults["hot"] = kw.pop("hot", False)
    defaults["breached"] = kw.pop("breached", False)
    defaults.update(kw)
    return build_sla_message(**defaults)


# ============================================================================
# 1. Templates: 8 variants
# ============================================================================


class TestTemplateVariants:
    def test_standard_warning_none(self):
        t = _msg(hot=False, breached=False, outreach_state="none")
        assert "pr\u00f3ximo a vencer" in t
        assert "160 de 180" in t
        assert "Hora l\u00edmite: 24/07/2026 12:00" in t

    def test_standard_warning_whatsapp_opened(self):
        t = _msg(hot=False, breached=False, outreach_state="whatsapp_opened")
        assert "detect\u00f3 que abriste WhatsApp" in t
        assert "Abrir WhatsApp no detiene el SLA" in t

    def test_standard_warning_whatsapp_sent(self):
        t = _msg(hot=False, breached=False, outreach_state="whatsapp_sent")
        assert "registr\u00f3 que enviaste un WhatsApp" in t
        assert "Enviar un WhatsApp no detiene el SLA" in t

    def test_standard_warning_phone_opened(self):
        t = _msg(hot=False, breached=False, outreach_state="phone_opened")
        assert "abriste el tel\u00e9fono del cliente" in t
        assert "Abrir el tel\u00e9fono" in t

    def test_standard_warning_call_without_result(self):
        t = _msg(hot=False, breached=False, outreach_state="call_without_result")
        assert "registr\u00f3 una llamada" in t
        assert "Realizar una llamada sin registrar su resultado no detiene el SLA" in t

    def test_standard_breached_none(self):
        t = _msg(hot=False, breached=True, outreach_state="none")
        assert "SLA vencido" in t
        assert "Venci\u00f3 el: 24/07/2026 12:00" in t

    def test_standard_breached_whatsapp_opened(self):
        t = _msg(hot=False, breached=True, outreach_state="whatsapp_opened")
        assert "detect\u00f3 que abriste WhatsApp" in t
        assert "Abrir WhatsApp no detiene el SLA" in t

    def test_standard_breached_whatsapp_sent(self):
        t = _msg(hot=False, breached=True, outreach_state="whatsapp_sent")
        assert "registr\u00f3 que enviaste un WhatsApp" in t
        assert "Enviar un WhatsApp no detiene el SLA" in t

    def test_standard_breached_phone_opened(self):
        t = _msg(hot=False, breached=True, outreach_state="phone_opened")
        assert "abriste el tel\u00e9fono" in t
        assert "Abrir el tel\u00e9fono del cliente no detiene el SLA" in t

    def test_standard_breached_call_without_result(self):
        t = _msg(hot=False, breached=True, outreach_state="call_without_result")
        assert "registr\u00f3 una llamada" in t
        assert "Realizar una llamada sin registrar su resultado no detiene el SLA" in t

    def test_hot_warning_none(self):
        t = _msg(hot=True, breached=False, outreach_state="none", elapsed_minutes=48)
        assert "Hot pr\u00f3ximo a vencer" in t and "48 de 60" in t

    def test_hot_warning_whatsapp_opened(self):
        t = _msg(hot=True, breached=False, outreach_state="whatsapp_opened", elapsed_minutes=46)
        assert "Hot" in t and "abriste WhatsApp" in t

    def test_hot_warning_phone_opened(self):
        t = _msg(hot=True, breached=False, outreach_state="phone_opened", elapsed_minutes=47)
        assert "Hot" in t and "abriste el tel\u00e9fono" in t

    def test_hot_breached_none(self):
        t = _msg(hot=True, breached=True, outreach_state="none", elapsed_minutes=65)
        assert "Hot con SLA vencido" in t and "65 minutos h\u00e1biles" in t

    def test_hot_breached_whatsapp_opened(self):
        t = _msg(hot=True, breached=True, outreach_state="whatsapp_opened", elapsed_minutes=62)
        assert "Hot" in t and "abriste WhatsApp" in t

    def test_hot_breached_whatsapp_sent(self):
        t = _msg(hot=True, breached=True, outreach_state="whatsapp_sent", elapsed_minutes=61)
        assert "enviaste un WhatsApp" in t


# ============================================================================
# 2. Uniform text: same explain + action for warning and breached
# ============================================================================


class TestUniformText:
    def test_warning_and_breached_same_explain(self):
        for state in EXPLAIN:
            w = build_sla_message(hot=False, breached=False, outreach_state=state,
                                  client_first_name="A", property_code="P",
                                  elapsed_minutes=160, deadline_display="x",
                                  lead_url="http://x")
            b = build_sla_message(hot=False, breached=True, outreach_state=state,
                                  client_first_name="A", property_code="P",
                                  elapsed_minutes=185, deadline_display="y",
                                  lead_url="http://x")
            w_paras = w.split("\n\n")
            b_paras = b.split("\n\n")
            # p[2] is the explanation paragraph — must match between warning and breached
            assert w_paras[2] == b_paras[2], f"explain differs for state={state}"

    def test_warning_and_breached_same_action(self):
        for state in ACTION:
            if state == "none":
                continue  # breached none uses "inmediatamente" variant
            w = build_sla_message(hot=False, breached=False, outreach_state=state,
                                  client_first_name="A", property_code="P",
                                  elapsed_minutes=160, deadline_display="x",
                                  lead_url="http://x")
            b = build_sla_message(hot=False, breached=True, outreach_state=state,
                                  client_first_name="A", property_code="P",
                                  elapsed_minutes=185, deadline_display="y",
                                  lead_url="http://x")
            w_paras = w.split("\n\n")
            b_paras = b.split("\n\n")
            assert w_paras[4] == b_paras[4], f"action differs for state={state}"

    def test_explain_none_exact(self):
        assert EXPLAIN["none"] == (
            "Todav\u00eda no existe una acci\u00f3n de contacto "
            "ni un resultado de gesti\u00f3n registrado para este lead."
        )

    def test_explain_whatsapp_opened_exact(self):
        assert EXPLAIN["whatsapp_opened"] == (
            "El CRM detect\u00f3 que abriste WhatsApp, pero a\u00fan "
            "no registraste el resultado del contacto."
        )

    def test_explain_whatsapp_sent_exact(self):
        assert EXPLAIN["whatsapp_sent"] == (
            "El CRM registr\u00f3 que enviaste un WhatsApp, pero "
            "a\u00fan no registraste el resultado del contacto."
        )

    def test_action_whatsapp_opened_exact(self):
        assert "Abrir WhatsApp no detiene el SLA." in ACTION["whatsapp_opened"]

    def test_action_whatsapp_sent_exact(self):
        assert "Enviar un WhatsApp no detiene el SLA." in ACTION["whatsapp_sent"]

    def test_action_phone_opened_exact(self):
        assert "Abrir el tel\u00e9fono del cliente no detiene el SLA." in ACTION["phone_opened"]

    def test_action_call_exact(self):
        assert "Realizar una llamada sin registrar su resultado no detiene el SLA." in ACTION["call_without_result"]

    def test_no_generic_accion_de_contacto(self):
        for state in ("whatsapp_opened", "whatsapp_sent", "phone_opened", "call_without_result"):
            assert "acci\u00f3n de contacto" not in EXPLAIN[state]

    def test_all_states_have_entries(self):
        states = {"none", "whatsapp_opened", "whatsapp_sent", "phone_opened",
                  "call_without_result", "email_opened", "email_sent"}
        assert set(EXPLAIN.keys()) == states
        assert set(ACTION.keys()) == states


# ============================================================================
# 3. Template structure
# ============================================================================


class TestTemplateStructure:
    def test_no_duplicate_cta(self):
        for hot in (False, True):
            for breached in (False, True):
                for state in EXPLAIN:
                    t = _msg(hot=hot, breached=breached, outreach_state=state)
                    assert t.count("Registra") <= 3, f"hot={hot} breached={breached} state={state}"

    def test_single_link(self):
        for hot in (False, True):
            for breached in (False, True):
                for state in ("none", "whatsapp_opened", "whatsapp_sent"):
                    t = _msg(hot=hot, breached=breached, outreach_state=state)
                    assert t.count("crm/lead-id/") == 1

    def test_no_mojibake(self):
        t = build_sla_message(
            hot=False, breached=False, client_first_name="Jos\u00e9",
            property_code="P001", elapsed_minutes=155,
            deadline_display="24/07/2026 11:30",
            lead_url="https://x.test/crm/lead-id/1", outreach_state="whatsapp_opened",
        )
        assert "gesti\u00f3n" in t and "l\u00edmite" in t and "h\u00e1biles" in t

    def test_url_present(self):
        t = _msg(lead_url="https://mycrm.cl/crm/lead-id/xyz")
        assert "https://mycrm.cl/crm/lead-id/xyz" in t

    def test_build_lead_url(self):
        import config
        base = config.Config.CRM_BASE_URL.rstrip("/")
        assert build_lead_url({"_id": "lead-42"}) == f"{base}/crm/lead-id/lead-42"

    def test_deadline_display_format(self):
        from pytz import timezone as ptz
        tz = ptz("America/Santiago")
        dt = datetime(2026, 7, 30, 16, 30, tzinfo=timezone.utc)
        assert build_deadline_display(dt, tz) == "30/07/2026 12:30"


# ============================================================================
# 4. Outreach classification (CALL_RESULT removed — fail-closed)
# ============================================================================


class TestOutreachClassification:
    def test_click_whatsapp_is_opened(self):
        start = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        assert classify_outreach_state(
            [{"type": "CLICK_WHATSAPP_LEAD", "timestamp": start}], assigned_at=start
        ) == "whatsapp_opened"

    def test_send_wa_confirmed_is_sent(self):
        start = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        assert classify_outreach_state(
            [{"type": "SEND_WA_LEAD", "timestamp": start, "confirmed": True}], assigned_at=start
        ) == "whatsapp_sent"

    def test_click_phone_is_not_call(self):
        start = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        assert classify_outreach_state(
            [{"type": "CLICK_PHONE_LEAD", "timestamp": start}], assigned_at=start
        ) == "phone_opened"

    def test_send_wa_without_confirm_ignored(self):
        start = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        assert classify_outreach_state(
            [{"type": "SEND_WA_LEAD", "timestamp": start}], assigned_at=start
        ) == "none"

    def test_call_completed_no_result_is_call_without_result(self):
        start = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        assert classify_outreach_state(
            [{"type": "CALL_COMPLETED_LEAD", "timestamp": start}], assigned_at=start
        ) == "call_without_result"

    def test_call_completed_with_result_ignored(self):
        start = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        assert classify_outreach_state(
            [{"type": "CALL_COMPLETED_LEAD", "timestamp": start, "result": "CONTACTADO"}],
            assigned_at=start,
        ) == "none"

    def test_call_result_never_classifies(self):
        """CALL_RESULT has 0 instances in production — fail-closed."""
        start = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        assert classify_outreach_state(
            [{"type": "CALL_RESULT", "timestamp": start}], assigned_at=start
        ) == "none"

    def test_click_phone_never_call_without_result(self):
        start = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        assert classify_outreach_state(
            [{"type": "CLICK_PHONE_LEAD", "timestamp": start}], assigned_at=start
        ) != "call_without_result"

    def test_highest_priority_wins(self):
        start = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        state = classify_outreach_state([
            {"type": "CLICK_WHATSAPP_LEAD", "timestamp": start + timedelta(minutes=1)},
            {"type": "SEND_WA_LEAD", "timestamp": start + timedelta(minutes=2), "confirmed": True},
        ], assigned_at=start)
        assert state == "whatsapp_sent"

    def test_event_before_assignment_ignored(self):
        start = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
        assert classify_outreach_state(
            [{"type": "CLICK_WHATSAPP_LEAD", "timestamp": start - timedelta(minutes=10)}],
            assigned_at=start,
        ) == "none"

    def test_mwr_is_whatsapp_sent(self):
        start = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        state = classify_outreach_state(
            [], assigned_at=start,
            mgmt_results=[{"result_type": "MESSAGE_SENT_WAITING_RESPONSE",
                           "occurred_at": start + timedelta(minutes=5)}],
        )
        assert state == "whatsapp_sent"

    def test_mwr_before_assignment_ignored(self):
        start = datetime(2026, 7, 24, 13, 0, tzinfo=timezone.utc)
        state = classify_outreach_state(
            [], assigned_at=start,
            mgmt_results=[{"result_type": "MESSAGE_SENT_WAITING_RESPONSE",
                           "occurred_at": start - timedelta(minutes=5)}],
        )
        assert state == "none"


# ============================================================================
# 5. Channel labels
# ============================================================================


class TestChannelLabels:
    def test_whatsapp_opened_label(self):
        assert outreach_channel_label("whatsapp_opened") == "abriste WhatsApp"

    def test_whatsapp_sent_label(self):
        assert outreach_channel_label("whatsapp_sent") == "enviaste un WhatsApp"

    def test_phone_opened_label(self):
        assert "tel\u00e9fono" in outreach_channel_label("phone_opened")

    def test_call_without_result_label(self):
        assert "llamada" in outreach_channel_label("call_without_result")

    def test_none_label_empty(self):
        assert outreach_channel_label("none") == ""


# ============================================================================
# 6. Business minutes
# ============================================================================


class TestBusinessMinutes:
    def test_same_day(self):
        start = CHILE_TZ.localize(datetime(2026, 7, 24, 9, 0))
        result = add_business_minutes(start, 60)
        local = result.astimezone(CHILE_TZ)
        assert local.hour == 10 and local.minute == 0

    def test_friday_to_monday(self):
        start = CHILE_TZ.localize(datetime(2026, 7, 24, 18, 30))
        result = add_business_minutes(start, 60)
        local = result.astimezone(CHILE_TZ)
        assert local.weekday() == 0


# ============================================================================
# 7. Exclusions
# ============================================================================


class TestExclusions:
    def test_synthetic_phone(self):
        assert _is_test_lead({"phone": "56900000000"}) is True

    def test_normal_phone(self):
        assert _is_test_lead({"phone": "56912345678"}) is False

    def test_excluded_origin(self):
        assert _is_test_lead({"phone": "56912345678", "lead_origin": "test"}) is True

    def test_first_name(self):
        assert _lead_first_name({"prospecto": {"nombre": "Ana Maria"}}) == "Ana"

    def test_first_name_missing(self):
        assert _lead_first_name({}) == "Cliente"

    def test_property_code(self):
        assert _lead_property_code({"prospecto": {"codigo": "P-123"}}) == "P-123"

    def test_property_code_missing(self):
        assert _lead_property_code({}) == "S/N"


# ============================================================================
# 8. Cutover
# ============================================================================


class TestCutover:
    @pytest.mark.asyncio
    async def test_missing_cutover_excludes_all(self, fake_db):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT", None)
            report = await evaluate_sla_alerts(db=fake_db)
        assert "missing_alert_cutover" in report["excluded_by_reason"]
        assert report["included"] == 0

    @pytest.mark.asyncio
    async def test_cycle_before_cutover_excluded(self, fake_db):
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "COLD")])
        fake_db["crm_assignment_cycles"] = FakeCollection([
            make_cycle("lead-1", "cycle-1", "user-1",
                       assigned_at=chile_dt(9, 0, 23).astimezone(timezone.utc))
        ])
        fake_db["crm_events"] = FakeCollection([])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(13, 0, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        assert report["excluded_by_reason"].get("before_alert_cutover", 0) == 1

    @pytest.mark.asyncio
    async def test_cycle_after_cutover_included(self, fake_db):
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "COLD")])
        fake_db["crm_assignment_cycles"] = FakeCollection([
            make_cycle("lead-1", "cycle-1", "user-1",
                       assigned_at=chile_dt(9, 0, 24).astimezone(timezone.utc))
        ])
        fake_db["crm_events"] = FakeCollection([])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(13, 0, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        assert report["included"] == 1


# ============================================================================
# 9. Management policy
# ============================================================================


class TestManagementPolicy:
    @pytest.mark.asyncio
    async def test_mwr_does_not_stop_sla(self, fake_db):
        assigned = chile_dt(9, 0, 24).astimezone(timezone.utc)
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "COLD")])
        fake_db["crm_assignment_cycles"] = FakeCollection([
            make_cycle("lead-1", "cycle-1", "user-1", assigned_at=assigned)
        ])
        fake_db["crm_events"] = FakeCollection([])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([{
            "assignment_cycle_id": "cycle-1",
            "result_type": "MESSAGE_SENT_WAITING_RESPONSE",
            "actor_user_id": "6989c6309dd2ba54e478196d",
            "occurred_at": chile_dt(9, 15, 24), "source": "crm_send_action",
        }])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(13, 0, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        assert len(report["alerts"]) == 1
        assert report["alerts"][0]["outreach_state"] == "whatsapp_sent"

    @pytest.mark.asyncio
    async def test_first_valid_management_at_alone_not_enough(self, fake_db):
        assigned = chile_dt(9, 0, 24).astimezone(timezone.utc)
        cycle = make_cycle("lead-1", "cycle-1", "user-1", assigned_at=assigned)
        cycle["first_valid_management_at"] = chile_dt(9, 15, 24)
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "COLD")])
        fake_db["crm_assignment_cycles"] = FakeCollection([cycle])
        fake_db["crm_events"] = FakeCollection([])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([{
            "assignment_cycle_id": "cycle-1",
            "result_type": "MESSAGE_SENT_WAITING_RESPONSE",
            "actor_user_id": "human-1",
            "occurred_at": chile_dt(9, 15, 24), "source": "crm_send_action",
        }])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(13, 0, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        assert len(report["alerts"]) == 1

    @pytest.mark.asyncio
    async def test_valid_result_after_mwr_stops_sla(self, fake_db):
        assigned = chile_dt(9, 0, 24).astimezone(timezone.utc)
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "COLD")])
        fake_db["crm_assignment_cycles"] = FakeCollection([
            make_cycle("lead-1", "cycle-1", "user-1", assigned_at=assigned)
        ])
        fake_db["crm_events"] = FakeCollection([])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([
            {"assignment_cycle_id": "cycle-1",
             "result_type": "MESSAGE_SENT_WAITING_RESPONSE",
             "actor_user_id": "human-1",
             "occurred_at": chile_dt(9, 15, 24), "source": "crm_send_action"},
            {"assignment_cycle_id": "cycle-1",
             "result_type": "EFFECTIVE_CONTACT",
             "actor_user_id": "human-1",
             "occurred_at": chile_dt(10, 0, 24), "source": "crm_quick_action"},
        ])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(13, 0, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        assert len(report["alerts"]) == 0

    @pytest.mark.asyncio
    async def test_valid_result_other_cycle_does_not_stop(self, fake_db):
        assigned = chile_dt(9, 0, 24).astimezone(timezone.utc)
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "COLD")])
        fake_db["crm_assignment_cycles"] = FakeCollection([
            make_cycle("lead-1", "cycle-1", "user-1", assigned_at=assigned)
        ])
        fake_db["crm_events"] = FakeCollection([])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([{
            "assignment_cycle_id": "other-cycle",
            "result_type": "EFFECTIVE_CONTACT",
            "actor_user_id": "human-1",
            "occurred_at": chile_dt(10, 0, 24), "source": "crm_quick_action",
        }])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(13, 0, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        assert len(report["alerts"]) == 1

    @pytest.mark.asyncio
    async def test_system_actor_not_management(self, fake_db):
        assigned = chile_dt(9, 0, 24).astimezone(timezone.utc)
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "COLD")])
        fake_db["crm_assignment_cycles"] = FakeCollection([
            make_cycle("lead-1", "cycle-1", "user-1", assigned_at=assigned)
        ])
        fake_db["crm_events"] = FakeCollection([{
            "lead_id": "lead-1", "type": "SEND_WA_LEAD", "actor": "system",
            "actor_type": "system", "result": "MENSAJE_ENVIADO",
            "timestamp": chile_dt(9, 15, 24).astimezone(timezone.utc), "confirmed": True,
        }])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(13, 0, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        assert len(report["alerts"]) == 1

    @pytest.mark.asyncio
    async def test_human_gestion_log_stops_sla(self, fake_db):
        assigned = chile_dt(9, 0, 24).astimezone(timezone.utc)
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "COLD")])
        fake_db["crm_assignment_cycles"] = FakeCollection([
            make_cycle("lead-1", "cycle-1", "user-1", assigned_at=assigned)
        ])
        fake_db["crm_events"] = FakeCollection([{
            "lead_id": "lead-1", "type": "GESTION_LOG", "actor": "user-1",
            "actor_type": "human", "result": "EFFECTIVE_CONTACT",
            "timestamp": chile_dt(9, 15, 24).astimezone(timezone.utc), "confirmed": True,
        }])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(13, 0, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        assert len(report["alerts"]) == 0


# ============================================================================
# 10. Async phone
# ============================================================================


class TestAsyncPhone:
    @pytest.mark.asyncio
    async def test_resolves_phone(self, fake_db):
        fake_db["usuarios"] = FakeCollection([
            {"nombre": "Erika Garrido", "is_active": True, "telefono": "+56911111111"},
        ])
        assert await _get_executive_phone_async(fake_db, "Erika Garrido") == "+56911111111"

    @pytest.mark.asyncio
    async def test_inactive_returns_none(self, fake_db):
        fake_db["usuarios"] = FakeCollection([
            {"nombre": "Inactiva", "is_active": False, "telefono": "+56922222222"},
        ])
        assert await _get_executive_phone_async(fake_db, "Inactiva") is None

    @pytest.mark.asyncio
    async def test_missing_returns_none(self, fake_db):
        assert await _get_executive_phone_async(fake_db, "Nadie") is None


# ============================================================================
# 11. E2E evaluator
# ============================================================================


class FakeCursor:
    def __init__(self, docs): self._docs = list(docs)
    async def to_list(self, length=None): return self._docs[:length]


class FakeCollection:
    def __init__(self, docs=None): self._docs = list(docs or [])
    def find(self, query, projection=None):
        matched = self._docs
        for key, expected in query.items():
            if key.startswith("$"): continue
            if isinstance(expected, dict):
                if "$in" in expected:
                    vals = [str(v) for v in expected["$in"]]
                    matched = [d for d in matched if str(d.get(key)) in vals]
                elif "$gte" in expected:
                    matched = [d for d in matched if d.get(key) is not None and d[key] >= expected["$gte"]]
                elif "$exists" in expected:
                    matched = [d for d in matched if (key in d) == expected["$exists"]]
            else:
                matched = [d for d in matched if d.get(key) == expected]
        return FakeCursor(matched)
    async def find_one(self, query, projection=None):
        for d in self._docs:
            ok = True
            for k, v in query.items():
                if isinstance(v, dict):
                    if "$exists" in v and (k in d) != v["$exists"]: ok = False
                elif d.get(k) != v: ok = False
            if ok: return d
        return None


class FakeDB(dict):
    def __missing__(self, key):
        self[key] = FakeCollection()
        return self[key]


@pytest.fixture
def fake_db():
    return FakeDB()


def chile_dt(hour, minute=0, day=24):
    return CHILE_TZ.localize(datetime(2026, 7, day, hour, minute))


def make_lead(lid="lead-1", temp="COLD", stage="NEW", phone="56912345678",
              exec_name="Ejecutiva", nombre="Cliente", codigo="P001"):
    return {
        "_id": lid, "phone": phone, "lead_temperature_effective": temp,
        "pipeline_stage": stage, "ejecutivo_asignado": exec_name,
        "prospecto": {"nombre": nombre, "codigo": codigo},
    }


def make_cycle(lid="lead-1", cid="cycle-1", user_id="user-1",
               assigned_at=None, reason="lead_created"):
    at = assigned_at or chile_dt(9, 0).astimezone(timezone.utc)
    return {
        "lead_id": lid, "assignment_cycle_id": cid,
        "assigned_to_user_id": user_id, "assigned_at": at,
        "unassigned_at": None, "cycle_status": "active", "reason": reason,
    }


class TestEvaluatorE2E:
    @pytest.mark.asyncio
    async def test_standard_breached_triggered(self, fake_db):
        assigned = chile_dt(9, 0, 24).astimezone(timezone.utc)
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "COLD")])
        fake_db["crm_assignment_cycles"] = FakeCollection([
            make_cycle("lead-1", "cycle-1", "user-1", assigned_at=assigned)
        ])
        fake_db["crm_events"] = FakeCollection([])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(12, 1, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        assert report["provider_calls"] == 0 and report["writes"] == 0
        assert len(report["alerts"]) == 1

    @pytest.mark.asyncio
    async def test_hot_warning_triggered(self, fake_db):
        assigned = chile_dt(9, 0, 24).astimezone(timezone.utc)
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "HOT")])
        fake_db["crm_assignment_cycles"] = FakeCollection([
            make_cycle("lead-1", "cycle-1", "user-1", assigned_at=assigned)
        ])
        fake_db["crm_events"] = FakeCollection([])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(9, 46, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        assert len(report["alerts"]) == 1

    @pytest.mark.asyncio
    async def test_closed_lead_excluded(self, fake_db):
        assigned = chile_dt(9, 0, 24).astimezone(timezone.utc)
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "COLD", "CLOSED_WON")])
        fake_db["crm_assignment_cycles"] = FakeCollection([
            make_cycle("lead-1", "cycle-1", "user-1", assigned_at=assigned)
        ])
        fake_db["crm_events"] = FakeCollection([])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(12, 1, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        assert report["excluded_by_reason"].get("lead_closed", 0) == 1

    @pytest.mark.asyncio
    async def test_test_lead_excluded(self, fake_db):
        assigned = chile_dt(9, 0, 24).astimezone(timezone.utc)
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "COLD", phone="56900000000")])
        fake_db["crm_assignment_cycles"] = FakeCollection([
            make_cycle("lead-1", "cycle-1", "user-1", assigned_at=assigned)
        ])
        fake_db["crm_events"] = FakeCollection([])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(12, 1, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        assert report["excluded_by_reason"].get("test_or_synthetic", 0) == 1

    @pytest.mark.asyncio
    async def test_message_has_date_and_url(self, fake_db):
        assigned = chile_dt(9, 0, 24).astimezone(timezone.utc)
        fake_db["leads"] = FakeCollection([make_lead("lead-1", "COLD", nombre="Carlos")])
        fake_db["crm_assignment_cycles"] = FakeCollection([
            make_cycle("lead-1", "cycle-1", "user-1", assigned_at=assigned)
        ])
        fake_db["crm_events"] = FakeCollection([])
        fake_db["usuarios"] = FakeCollection([])
        fake_db["crm_management_results"] = FakeCollection([])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_settings.CRM_SLA_ALERT_CUTOVER_AT",
                       datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc))
            mp.setattr("chatbot.crm_sla_alert_evaluator.utc_now",
                       lambda: chile_dt(12, 1, 24).astimezone(timezone.utc))
            report = await evaluate_sla_alerts(db=fake_db, limit_cycles=100)
        msg = report["alerts"][0]["message"]
        assert "Carlos" in msg
        assert "/crm/lead-id/" in msg
        assert "/07/2026" in msg


# ============================================================================
# 12. Constants
# ============================================================================


def test_alert_constants():
    assert THRESHOLD_WARNING_NORMAL == 150
    assert THRESHOLD_BREACHED_NORMAL == 180
    assert THRESHOLD_WARNING_HOT == 45
    assert THRESHOLD_BREACHED_HOT == 60
    assert MESSAGE_DOMAIN == "crm_sla_alert"


def test_sla_stop_results_excludes_mwr():
    assert "MESSAGE_SENT_WAITING_RESPONSE" not in SLA_STOP_RESULTS


def test_outreach_results_includes_mwr():
    assert "MESSAGE_SENT_WAITING_RESPONSE" in OUTREACH_RESULTS


def test_sla_stop_results_has_valid_types():
    for r in ("EFFECTIVE_CONTACT", "CALL_NO_ANSWER", "INVALID_NUMBER",
              "FOLLOW_UP_REQUESTED", "SCHEDULE_FOLLOW_UP",
              "NOT_INTERESTED", "DISCARDED_VALID_REASON"):
        assert r in SLA_STOP_RESULTS
