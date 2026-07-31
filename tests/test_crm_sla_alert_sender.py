"""Tests for CRM SLA Alert Sender."""
import pytest

from chatbot.crm_sla_alert_sender import FakeSender, FailingSender, get_sender, SenderResult


class TestFakeSender:
    @pytest.mark.asyncio
    async def test_confirmed_success(self):
        s = FakeSender("confirmed_success", "wa-001")
        r: SenderResult = await s("+56911111111", "msg")
        assert r.outcome == "confirmed_success" and r.provider_message_id == "wa-001"

    @pytest.mark.asyncio
    async def test_rejected(self):
        s = FakeSender("rejected_before_acceptance")
        r: SenderResult = await s("+56900000000", "msg")
        assert r.outcome == "rejected_before_acceptance" and r.provider_message_id is None

    @pytest.mark.asyncio
    async def test_delivery_unknown(self):
        s = FakeSender("delivery_unknown")
        r: SenderResult = await s("+56900000000", "msg")
        assert r.outcome == "delivery_unknown"


class TestFailingSender:
    @pytest.mark.asyncio
    async def test_timeout(self):
        with pytest.raises(TimeoutError):
            await FailingSender(TimeoutError("t"))("+56911111111", "test")

    @pytest.mark.asyncio
    async def test_permanent_error(self):
        with pytest.raises(RuntimeError):
            await FailingSender(RuntimeError("500"))("+56911111111", "test")


class TestGetSender:
    @pytest.mark.asyncio
    async def test_live_send_false_null(self):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("chatbot.crm_sla_alert_sender.CRM_SLA_ALERTS_LIVE_SEND", False)
            r: SenderResult = await get_sender()("+56911111111", "test")
        assert r.outcome == "delivery_unknown"

    @pytest.mark.asyncio
    async def test_injected_used(self):
        fake = FakeSender("confirmed_success", "inj")
        r: SenderResult = await get_sender(fake)("+56911111111", "test")
        assert r.outcome == "confirmed_success"


class TestNoRealImports:
    def test_no_notification_service(self):
        import chatbot.crm_sla_alert_sender as mod
        lines = [l for l in open(mod.__file__).read().split('\n') if l.startswith(('import ', 'from '))]
        joined = '\n'.join(lines)
        assert "NotificationService" not in joined
        assert "crm_hot_delivery" not in joined
        assert "notification_service" not in joined
        assert "webhook" not in joined

    def test_no_whatsapp_client(self):
        import chatbot.crm_sla_alert_sender as mod
        lines = [l for l in open(mod.__file__).read().split('\n') if l.startswith(('import ', 'from '))]
        joined = '\n'.join(lines)
        assert "whatsapp_client" not in joined
