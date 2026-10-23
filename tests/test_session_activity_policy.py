"""Only session-handling changes belong in the five-minute optimization."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "templates/leads_dashboard.html").read_text(encoding="utf-8")
CRM_LIST = (ROOT / "templates/crm_leads_list.html").read_text(encoding="utf-8")
CRM_DETAIL = (ROOT / "templates/crm_lead_detail.html").read_text(encoding="utf-8")
SESSION = (ROOT / "static/js/crm_session_activity.js").read_text(encoding="utf-8")


def test_session_control_is_shared_once_and_five_second_loop_is_removed():
    for page in (DASHBOARD, CRM_LIST, CRM_DETAIL):
        assert page.count("crm_session_activity.js") == 1
        assert "}, 5000);" not in page
        assert "fetch('/api/session/renew'" not in page
        assert 'id="sessionWarningModal"' in page
        assert "updateActivity();" in page


def test_crm_detail_no_longer_has_conflicting_thirty_minute_session():
    assert "sessionManager.init()" not in CRM_DETAIL
    assert "sessionManager.extendSession()" not in CRM_DETAIL
    assert 'id="sessionModal"' not in CRM_DETAIL


def test_five_minute_watchdog_and_exact_warning_are_independent():
    assert "const CHECK_INTERVAL_MS = 5 * 60 * 1000;" in SESSION
    assert "const SESSION_LIFETIME_MS = 120 * 60 * 1000;" in SESSION
    assert "const WARNING_THRESHOLD_MS = 2 * 60 * 1000;" in SESSION
    assert "const RENEWAL_INTERVAL_MS = 10 * 60 * 1000;" in SESSION
    assert "setInterval(checkSession, CHECK_INTERVAL_MS)" in SESSION
    assert "milestoneTimer = setTimeout(" in SESSION
    assert "setInterval(checkSession, 1000)" in SESSION


def test_separate_dashboard_and_crm_polling_changes_are_not_included():
    assert "if (!document.hidden) loadData('refresh');" not in DASHBOARD
    assert "CRM_POLL_INTERVAL_MS = 20000;" in CRM_LIST
    assert "checkCrmUpdates" in CRM_LIST
