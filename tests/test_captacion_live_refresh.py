from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_detail_broadcasts_successful_management_to_captacion_list():
    source = (ROOT / "templates" / "captacion_detail.html").read_text(encoding="utf-8")

    assert "captacionMetricsUpdatedAt" in source
    assert "notifyCaptacionMetricsUpdated();" in source
    assert "if (result !== 'cancel') notifyCaptacionMetricsUpdated();" in source


def test_captacion_list_refreshes_when_detail_management_changes_metrics():
    source = (ROOT / "templates" / "captacion_list.html").read_text(encoding="utf-8")

    assert "window.addEventListener('storage'" in source
    assert "event.key === 'captacionMetricsUpdatedAt'" in source
    assert "window.location.reload();" in source


def test_captacion_popovers_close_when_scrolling():
    source = (ROOT / "templates" / "captacion_list.html").read_text(encoding="utf-8")

    assert "document.addEventListener('scroll'" in source
    assert "closeCaptacionPopovers()" in source


def test_management_invalidates_the_full_current_goal_snapshot():
    source = (ROOT / "webhook.py").read_text(encoding="utf-8")

    assert "def _invalidate_captacion_goal_cache" in source
    assert "def _delete_current_captacion_goal_snapshots" in source
    assert "_invalidate_captacion_goal_cache()" in source
    assert "_delete_current_captacion_goal_snapshots" in source


def test_current_goal_never_uses_persistent_snapshot_as_display_value():
    source = (ROOT / "webhook.py").read_text(encoding="utf-8")

    assert "El período actual debe salir siempre del ledger fresco" in source
    assert "snapshot_can_be_used = snapshot and bool(goal_period_start or goal_period_end)" in source


def test_captacion_kpis_include_new_portals_without_hardcoding_names():
    source = (ROOT / "webhook.py").read_text(encoding="utf-8")

    assert 'CAPTACION_KPI_CACHE_VERSION = "v13"' in source
    assert source.count('"origen": {"$exists": True, "$nin": [None, ""]}') >= 2
    assert '"origen": {"$in": ["toctoc", "yapo"]}' not in source


def test_snapshot_dates_without_timezone_are_treated_as_utc():
    source = (ROOT / "webhook.py").read_text(encoding="utf-8")

    assert "snapshot_timestamp.replace(tzinfo=timezone.utc)" in source
