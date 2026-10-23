"""Contract tests for user-driven CRM refresh and session activity.

These checks keep the intentionally disabled polling from returning silently.
Backend SLA workers, permissions and transactional guards are unchanged.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "templates/leads_dashboard.html").read_text(encoding="utf-8")
CRM_LIST = (ROOT / "templates/crm_leads_list.html").read_text(encoding="utf-8")
CRM_DETAIL = (ROOT / "templates/crm_lead_detail.html").read_text(encoding="utf-8")
SESSION = (ROOT / "static/js/crm_session_activity.js").read_text(encoding="utf-8")


def test_commercial_dashboard_loads_on_open_or_explicit_filter_only():
    assert "if (INITIAL_TAB === 'executive') loadData('full');" in DASHBOARD
    assert "loadData('refresh');" in DASHBOARD  # explicit filter controls still work
    assert "}, 900000);" not in DASHBOARD
    assert "if (!document.hidden) loadData('refresh');" not in DASHBOARD
    assert "window.setInterval(sync, 300)" not in DASHBOARD
    assert "new MutationObserver(sync)" in DASHBOARD


def test_crm_lead_list_never_polls_in_background():
    assert "CRM_POLL_INTERVAL_MS" not in CRM_LIST
    assert "checkCrmUpdates" not in CRM_LIST
    assert "crm/check-updates?since=" not in CRM_LIST
    assert "if (event.persisted) window.location.reload();" in CRM_LIST


def test_crm_still_refreshes_explicit_actions_and_mutations():
    assert 'title="Consultar los responsables y estados SLA actuales"' in CRM_LIST
    assert 'onclick="refreshCrmList({ force: true })"' in CRM_LIST
    assert "crm:mutation-complete" in CRM_LIST
    assert "CRM_MUTATION_ENDPOINTS" in CRM_LIST
    assert "fetch(`/crm/partial" in CRM_LIST


def test_one_shared_session_manager_and_no_duplicate_detail_timer():
    for page in (DASHBOARD, CRM_LIST, CRM_DETAIL):
        assert page.count("crm_session_activity.js") == 1
        assert "}, 5000);" not in page
        assert "fetch('/api/session/renew'" not in page
    assert "sessionManager.init()" not in CRM_DETAIL
    assert 'id="sessionModal"' not in CRM_DETAIL


def test_session_checks_are_local_minutely_and_renew_active_only():
    assert "const CHECK_INTERVAL_MS = 60 * 1000;" in SESSION
    assert "const RENEWAL_INTERVAL_MS = 10 * 60 * 1000;" in SESSION
    assert "setInterval(checkSession, CHECK_INTERVAL_MS)" in SESSION
    assert "setInterval(checkSession, 1000)" in SESSION  # warning countdown only
    assert "if (expired || document.hidden || renewalInFlight) return;" in SESSION
    assert "now - lastActivityAt >= RENEWAL_INTERVAL_MS" in SESSION
    assert "const RETRY_INTERVAL_MS = 60 * 1000;" in SESSION
    assert "window.updateActivity =" in SESSION
    assert "if (now - lastActivityAt >= SESSION_LIFETIME_MS)" in SESSION
