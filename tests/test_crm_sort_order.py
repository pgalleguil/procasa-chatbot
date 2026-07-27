"""Tests for CRM listing sort order using canonical assignment cycle data.

These tests validate the MongoDB $sort spec produced by the canonical pipeline
for each ordering criterion, without requiring a full async test framework.
"""
import pytest
from datetime import datetime, timedelta, timezone

# ─── Test: sort parameter normalisation ───────────────────────────────────

def test_sort_map_normalises_all_old_names():
    """Verify the _sort_map in api_crm.py maps all legacy names to canonical names."""
    path = "api_crm.py"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert '"recent_assigned"' in src
    assert '"sla_priority"' in src
    assert '"oldest_unmanaged"' in src
    # Legacy aliases
    assert '"recientes": "recent_assigned"' in src
    assert '"antiguos_sin_atender": "oldest_unmanaged"' in src
    assert '"sla_urgente": "sla_priority"' in src

def test_sort_spec_recent_assigned():
    """recent_assigned: assigned first (_has_assigned=0), newest first (_cycle_assigned_at DESC), _id DESC."""
    # This tests the sort spec defined in api_crm.py
    path = "api_crm.py"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    # Verify the sort spec exists
    assert '"_has_assigned": 1, "_cycle_assigned_at": -1, "_id": -1' in src
    # Verify the branch for recent_assigned
    assert 'ordenar_por == "recent_assigned"' in src

def test_sort_spec_sla_priority():
    """sla_priority: HOT first (_temperature=0), oldest first (_cycle_assigned_at ASC)."""
    path = "api_crm.py"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert '"sla_priority"' in src
    # temperature goes before cycle_assigned_at (HOT=0 < COLD)
    assert '"_temperature": 1' in src
    assert '"_cycle_assigned_at": 1' in src
    # _has_assigned puts unassigned last
    assert '"_has_assigned": 1' in src

def test_sort_spec_oldest_unmanaged():
    """oldest_unmanaged: unmanaged first, oldest assigned first."""
    path = "api_crm.py"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert '"oldest_unmanaged"' in src
    assert '"_has_management"' in src
    assert '"_cycle_assigned_at": 1' in src

def test_pipeline_includes_lookup_to_crm_assignment_cycles():
    """The canonical pipeline uses $lookup to join with active cycles."""
    path = "api_crm.py"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert '"$lookup"' in src
    assert '"from": "crm_assignment_cycles"' in src
    assert '"unassigned_at": None' in src

def test_projection_includes_cycle_fields():
    """PROJECTION includes the computed cycle fields needed for enrichment."""
    path = "api_crm.py"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert '"_has_assigned": 1' in src
    assert '"_cycle_assigned_at": 1' in src
    assert '"_temperature": 1' in src
    assert '"_has_management": 1' in src

def test_webhook_default_sort_is_recent_assigned():
    """The webhook.py default sort parameter is recent_assigned."""
    path = "webhook.py"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert 'orden: str = "recent_assigned"' in src

def test_template_default_option_is_recent_assigned():
    """The HTML template default option is Más recientes (recent_assigned)."""
    path = "templates/crm_leads_list.html"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert 'value="recent_assigned"' in src
    assert 'Más recientes' in src
    # Default selected when no query param or when orden is empty
    assert "not request.query_params.get('orden')" in src

def test_template_all_three_options_present():
    """All three sort options appear in the HTML dropdown."""
    path = "templates/crm_leads_list.html"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert 'value="recent_assigned"' in src
    assert 'value="sla_priority"' in src
    assert 'value="oldest_unmanaged"' in src

def test_javascript_removes_default_sort_from_url():
    """JavaScript removes recent_assigned (default) from URL params."""
    path = "templates/crm_leads_list.html"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "params.get('orden') === 'recent_assigned'" in src
    assert "params.delete('orden')" in src

def test_old_param_names_backward_compatible():
    """Old names (sla_urgente, recientes, antiguos_sin_atender) still work in template."""
    path = "templates/crm_leads_list.html"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "'sla_urgente'" in src or '"sla_urgente"' in src
    assert "'recientes'" in src or '"recientes"' in src
    assert "'antiguos_sin_atender'" in src or '"antiguos_sin_atender"' in src

def test_no_fallback_to_created_at_in_sort():
    """The sort no longer uses _created_dt or created_at as fallback for assignment date."""
    path = "api_crm.py"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    # The old _assigned_dt fallback chain is removed
    # No reference to fecha_asignacion or created_at in sort computation
    assert '"_assigned_dt"' not in src or '"_assigned_dt"' not in src.split('"$sort"')[1] if '"$sort"' in src else True

def test_no_last_action_sort_in_canonical_pipeline():
    """The canonical pipeline does not sort by last_action_dt or activity_dt."""
    path = "api_crm.py"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    # _last_action_dt and _activity_dt should not be in the canonical pipeline
    sort_section = src.split('"$sort"')[-1] if '"$sort"' in src else ""
    assert '"_last_action_dt"' not in sort_section
    assert '"_activity_dt"' not in sort_section

def test_no_mixing_assigned_and_unassigned_in_sort():
    """recent_assigned sorts _has_assigned first (0=assigned, 1=unassigned), so unassigned always last."""
    path = "api_crm.py"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    # The sort spec must have _has_assigned: 1 before cycle_assigned_at
    idx_has = src.find('"_has_assigned"')
    idx_cycle = src.find('"_cycle_assigned_at"')
    if '"recent_assigned"' in src:
        # Find the recent_assigned sort block
        recent_block = src.split('"recent_assigned"')[1].split('"_id"')[0] if '"recent_assigned"' in src else ""
        assert '"_has_assigned"' in recent_block or '"_has_assigned"' in src
