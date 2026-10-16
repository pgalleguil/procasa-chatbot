"""Auditable, read-only monthly executive control metrics.

The module deliberately keeps assignment episodes, dated events, and month-end
stock as separate populations.  It does not modify CRM policy or records.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable, Mapping
import unicodedata

from chatbot.constants import CHILE_TZ
from chatbot.crm_metrics import (
    CONTACT_ATTEMPT_RESULTS, EFFECTIVE_CONTACT_RESULTS, calculate_sla,
    coerce_utc_datetime, event_evidence, normalize_result,
)


COVERED_MONTHS = {"2026-08", "2026-09"}
EXCLUDED_ORIGINS = {"test", "backfill", "reconciliation", "automated_historical", "e2e_test", "smoke_test"}
EXCLUDED_REASONS = EXCLUDED_ORIGINS | {"data_repair_orphan_lead_missing", "orphan_lead_not_found_repair"}
CONTROL_NON_BROKER_IDENTITIES = {"Control Owner"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _person_identity(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value)).encode("ascii", "ignore").decode("ascii")
    return " ".join(normalized.casefold().split())


def _is_control_non_broker(value: Any) -> bool:
    """Keep control-management records in CRM, but omit them from broker cohorts."""
    return _person_identity(value) in CONTROL_NON_BROKER_IDENTITIES


def _identity(value: Any) -> str:
    if isinstance(value, Mapping) and "$oid" in value:
        return str(value["$oid"])
    return str(value or "")


def _is_test(cycle: Mapping[str, Any], lead: Mapping[str, Any] | None) -> bool:
    lead = lead or {}
    origin = _text(cycle.get("cycle_origin") or cycle.get("reason")).casefold()
    if origin in EXCLUDED_ORIGINS or _text(cycle.get("closed_reason")).casefold() in EXCLUDED_REASONS:
        return True
    if lead.get("_test_lead") is True or lead.get("is_test") is True:
        return True
    if _text(lead.get("phone")) in {"0000000000", "0000000000", "0000000000"}:
        return True
    for source in (lead.get("lead_origin"), lead.get("origin"), (lead.get("prospecto") or {}).get("origen")):
        if _text(source).casefold() in EXCLUDED_ORIGINS:
            return True
    return False


def _exclusion_category(cycle: Mapping[str, Any], lead: Mapping[str, Any] | None) -> str | None:
    lead = lead or {}
    origin = _text(cycle.get("cycle_origin") or cycle.get("reason")).casefold()
    lead_origin = {_text(value).casefold() for value in (
        lead.get("lead_origin"), lead.get("origin"), (lead.get("prospecto") or {}).get("origen"),
    )}
    if (lead.get("_test_lead") is True or lead.get("is_test") is True
            or _text(lead.get("phone")) in {"0000000000", "0000000000", "0000000000"}
            or origin in {"test", "e2e_test", "smoke_test"}
            or lead_origin.intersection({"test", "e2e_test", "smoke_test"})):
        return "test"
    closed_reason = _text(cycle.get("closed_reason")).casefold()
    if origin in EXCLUDED_ORIGINS or closed_reason in EXCLUDED_REASONS:
        return "technical"
    repair_recorded = (
        _text(cycle.get("assigned_by")).casefold() == "policy_repair"
        or bool(cycle.get("policy_repair_id") or cycle.get("policy_repair_version")
                or cycle.get("policy_repair_reason"))
    )
    if repair_recorded:
        previous_owner = _text(cycle.get("policy_repair_previous_owner_user_id"))
        selected_owner = _text(
            cycle.get("policy_repair_selected_user_id") or cycle.get("assigned_to_user_id")
        )
        # A repair is excluded only when the record does not prove a change in
        # commercial responsibility.  The synthetic cycle ID/prefix alone is
        # not sufficient: repaired assignments with distinct source and target
        # owners remain eligible commercial episodes.
        if not previous_owner or not selected_owner or previous_owner == selected_owner:
            return "technical"
    return None


def _median(values: Iterable[float]) -> float | None:
    values = list(values)
    return round(float(median(values)), 1) if values else None


def _management_temperature(
    cycle: Mapping[str, Any], lead: Mapping[str, Any] | None, first_at: datetime,
) -> tuple[str | None, str | None]:
    """Resolve the lead type for first-management P50 from timestamped evidence.

    ``temperature_at_assignment`` is mutable in the CRM when an active lead is
    escalated. A persisted Hot transition and the lead's timestamped history
    therefore take precedence over treating that field as an immutable snapshot.
    """
    assigned = coerce_utc_datetime(cycle.get("assigned_at"))
    if not assigned:
        return None, "Asignación sin fecha verificable."

    history: list[tuple[datetime, str]] = []
    for entry in (lead or {}).get("temperature_history") or []:
        if not isinstance(entry, Mapping):
            continue
        at = coerce_utc_datetime(entry.get("at") or entry.get("timestamp") or entry.get("changed_at"))
        value = _text(entry.get("value") or entry.get("temperature")).upper()
        if at and value in {"HOT", "NORMAL", "COLD"}:
            history.append((at, value))
    history.sort(key=lambda item: item[0])

    before_assignment = [item for item in history if item[0] <= assigned]
    hot_before_assignment = bool(before_assignment and before_assignment[-1][1] == "HOT")
    hot_by_first_management = any(
        value == "HOT" and assigned <= at <= first_at for at, value in history
    )
    persisted_hot_start = coerce_utc_datetime(
        cycle.get("hot_started_at") or cycle.get("temperature_transitioned_at")
    )
    valid_hot_start = bool(persisted_hot_start and persisted_hot_start >= assigned)
    hot_transition_by_first = bool(valid_hot_start and persisted_hot_start <= first_at)
    if hot_before_assignment or hot_by_first_management or hot_transition_by_first:
        source = "temperature_history" if hot_before_assignment or hot_by_first_management else "hot_started_at"
        return "HOT", source

    temperature = _text(cycle.get("temperature_at_assignment")).upper()
    # A recorded later escalation proves the lead had not yet escalated when
    # this first management occurred. COLD and NORMAL share the Normal SLA tier.
    if valid_hot_start and persisted_hot_start > first_at:
        return "NORMAL", "hot_started_at posterior a la primera gestión"
    if temperature in {"NORMAL", "COLD"}:
        return "NORMAL", "temperature_at_assignment sin escalación Hot previa"
    if temperature == "HOT":
        if before_assignment and before_assignment[-1][1] in {"NORMAL", "COLD"}:
            return "NORMAL", "temperature_history al momento de asignación"
        return None, "Temperatura HOT mutable sin marca histórica de inicio."
    if before_assignment:
        return ("HOT" if before_assignment[-1][1] == "HOT" else "NORMAL"), "temperature_history al momento de asignación"
    return None, "No existe temperatura histórica verificable para el episodio."


def _first_management(
    cycle: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
    results: Iterable[Mapping[str, Any]],
) -> tuple[datetime | None, str | None]:
    """Resolve the first canonical human-management timestamp for one cycle."""
    # Human management can be accredited after the commercial assignment but
    # before the persisted SLA clock starts (for example, notification delay).
    # Keep episode ownership as the lower bound; SLA calculation separately
    # clamps pre-clock elapsed minutes to zero.
    assigned = coerce_utc_datetime(cycle.get("assigned_at"))
    cycle_id = _text(cycle.get("assignment_cycle_id"))
    lead_id = _identity(cycle.get("lead_id"))
    if not cycle_id or not assigned:
        return None, None
    candidates: list[tuple[datetime, str]] = []
    for event in events:
        if _text(event.get("assignment_cycle_id")) != cycle_id:
            continue
        if event.get("lead_id") is not None and _identity(event.get("lead_id")) != lead_id:
            continue
        evidence = event_evidence(event)
        if not evidence.get("management"):
            continue
        at = coerce_utc_datetime(event.get("timestamp") or event.get("occurred_at"))
        if at and (not assigned or at >= assigned):
            candidates.append((at, "crm_events"))
    for result in results:
        if _text(result.get("assignment_cycle_id")) != cycle_id:
            continue
        if result.get("lead_id") is not None and _identity(result.get("lead_id")) != lead_id:
            continue
        if result.get("status") not in (None, "completed") or not result.get("actor_user_id"):
            continue
        at = coerce_utc_datetime(result.get("occurred_at"))
        if at and (not assigned or at >= assigned):
            candidates.append((at, "crm_management_results"))
    marker = coerce_utc_datetime(cycle.get("first_valid_management_at"))
    if marker and (not assigned or marker >= assigned):
        # The cycle marker is the CRM's canonical denormalized first-management
        # field. An earlier valid event/result wins when it proves a correction.
        candidates.append((marker, "cycle.first_valid_management_at"))
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: item[0])
    return candidates[0]


def build_month_metrics(
    *,
    cycles: Iterable[Mapping[str, Any]],
    leads: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
    results: Iterable[Mapping[str, Any]],
    sla_audits: Iterable[Mapping[str, Any]],
    executive: str | None,
    as_of: datetime,
) -> dict[str, Any]:
    """Build metrics for episodes that started in exactly one covered month."""
    as_of = coerce_utc_datetime(as_of) or datetime.now(timezone.utc)
    deduped_cycles: dict[str, Mapping[str, Any]] = {}
    duplicate_cycle_records = 0
    for cycle in cycles:
        cycle_id = _text(cycle.get("assignment_cycle_id"))
        if not cycle_id:
            continue
        previous = deduped_cycles.get(cycle_id)
        if previous is None:
            deduped_cycles[cycle_id] = cycle
            continue
        duplicate_cycle_records += 1
        completeness = lambda row: sum(row.get(field) not in (None, "") for field in (
            "lead_id", "assigned_at", "assigned_to_display_name", "temperature_at_assignment",
            "reassignment_reason", "assignment_number", "first_valid_management_at",
        ))
        if completeness(cycle) > completeness(previous):
            deduped_cycles[cycle_id] = cycle
    lead_by_id = {_identity(lead.get("_id")): lead for lead in leads}
    events_by_cycle: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    results_by_cycle: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("assignment_cycle_id"):
            events_by_cycle[_text(event.get("assignment_cycle_id"))].append(event)
    for result in results:
        if result.get("assignment_cycle_id"):
            results_by_cycle[_text(result.get("assignment_cycle_id"))].append(result)

    audits_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    audits_by_target: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    audits_by_lead_time: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    unique_audits: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    duplicate_audit_records = 0
    for audit in sla_audits:
        transition_key = (
            _text(audit.get("source_cycle_id")),
            _text(audit.get("new_cycle_id") or audit.get("destination_cycle_id")),
            _text(audit.get("reassigned_at")),
        )
        if not any(transition_key):
            transition_key = ("audit-id", _identity(audit.get("_id")), "")
        if transition_key in unique_audits:
            duplicate_audit_records += 1
            continue
        unique_audits[transition_key] = audit
    for audit in unique_audits.values():
        if audit.get("event_type") == "SLA_REASSIGNMENT_COMMITTED":
            audits_by_source[_text(audit.get("source_cycle_id"))].append(audit)
            target_id = _text(audit.get("new_cycle_id") or audit.get("destination_cycle_id"))
            if target_id:
                audits_by_target[target_id].append(audit)
            at = coerce_utc_datetime(audit.get("reassigned_at"))
            if at:
                audits_by_lead_time[(_identity(audit.get("lead_id")), at.isoformat())].append(audit)

    selected = _text(executive).casefold()
    included: list[Mapping[str, Any]] = []
    excluded_tests = 0
    excluded_technical = 0
    excluded_non_broker = 0
    for cycle in deduped_cycles.values():
        lead = lead_by_id.get(_identity(cycle.get("lead_id")))
        exclusion = _exclusion_category(cycle, lead)
        if exclusion:
            excluded_tests += int(exclusion == "test")
            excluded_technical += int(exclusion == "technical")
            continue
        if _is_control_non_broker(cycle.get("assigned_to_display_name")):
            excluded_non_broker += 1
            continue
        if selected and _text(cycle.get("assigned_to_display_name")).casefold() != selected:
            continue
        included.append(cycle)

    unique_leads = {_identity(row.get("lead_id")) for row in included if row.get("lead_id") is not None}
    original = rescue = other_transfer = unknown_assignment = 0
    hot = normal = unknown_temperature = 0
    within_hot = outside_hot = within_normal = outside_normal = insufficient = 0
    management_minutes: list[float] = []
    management_minutes_hot: list[float] = []
    management_minutes_normal: list[float] = []
    p50_unverified_temperature = 0
    p50_unverified_time = 0
    p50_not_managed = 0
    episode_rows: list[dict[str, Any]] = []
    mismatch_count = 0
    mismatch_confirmed_by_event = 0
    attempt_cycle_ids: set[str] = set()
    effective_cycle_ids: set[str] = set()

    for cycle in included:
        cycle_id = _text(cycle.get("assignment_cycle_id"))
        lead_id = _identity(cycle.get("lead_id"))
        reason = _text(cycle.get("reassignment_reason")).casefold()
        assignment_at = coerce_utc_datetime(cycle.get("assigned_at"))
        reassignment_meta = cycle.get("reassignment_meta") or {}
        source_cycle_id = _text(reassignment_meta.get("previous_cycle_id"))
        audit_matches = audits_by_target.get(cycle_id, [])
        if not audit_matches:
            audit_matches = audits_by_source.get(source_cycle_id, []) if source_cycle_id else []
        if not audit_matches and assignment_at:
            audit_matches = audits_by_lead_time.get((lead_id, assignment_at.isoformat()), [])
        assignment_number = cycle.get("assignment_number")
        if reason == "sla_reassignment" and audit_matches:
            assignment_kind = "sla_rescue"
        elif reason or source_cycle_id or (isinstance(assignment_number, int) and assignment_number > 1):
            assignment_kind = "other_transfer"
        elif str(assignment_number or "").strip() == "1" or (
            not reason and not source_cycle_id
            and _text(cycle.get("assigned_by")).casefold() in {
                "commercial_intake", "prop360_extractor", "initial_assignment", "round_robin",
            }
        ):
            assignment_kind = "original"
        else:
            assignment_kind = "unknown"
        if assignment_kind == "original":
            original += 1
        elif assignment_kind == "sla_rescue":
            rescue += 1
        elif assignment_kind == "other_transfer":
            other_transfer += 1
        else:
            unknown_assignment += 1

        assigned_at = coerce_utc_datetime(cycle.get("sla_started_at") or cycle.get("assigned_at"))
        first_at, first_source = _first_management(
            cycle, events_by_cycle.get(cycle_id, []), results_by_cycle.get(cycle_id, [])
        )
        episode_results = results_by_cycle.get(cycle_id, [])
        valid_episode_results = [
            row for row in episode_results
            if row.get("status") in (None, "completed") and row.get("actor_user_id")
        ]
        result_types = {
            normalize_result(row.get("result_type"))
            for row in valid_episode_results
        }
        has_attempt = bool(result_types.intersection(CONTACT_ATTEMPT_RESULTS)) or any(
            event_evidence(event).get("contact_attempt") for event in events_by_cycle.get(cycle_id, [])
        )
        # For the card's "contacto efectivo" count, use the CRM result
        # classification only. A CRM event can prove management or an attempt,
        # but must not silently promote an episode to an effective contact.
        has_effective_contact = bool(result_types.intersection(EFFECTIVE_CONTACT_RESULTS))
        if has_attempt:
            attempt_cycle_ids.add(cycle_id)
        if has_effective_contact:
            effective_cycle_ids.add(cycle_id)
        management_before_sla_start = bool(first_at and assigned_at and first_at < assigned_at)
        if first_at and assigned_at:
            from chatbot.utils import calculate_business_minutes
            minutes = max(0.0, calculate_business_minutes(
                assigned_at.astimezone(CHILE_TZ), first_at.astimezone(CHILE_TZ)
            ))
            management_minutes.append(minutes)
        else:
            minutes = None

        p50_temperature, p50_temperature_source = (
            _management_temperature(cycle, lead, first_at) if first_at else (None, None)
        )
        if first_at and minutes is None:
            p50_unverified_time += 1
            p50_exclusion_reason = "Primera gestión acreditada, pero falta inicio SLA o fecha calculable."
        elif not first_at:
            p50_not_managed += 1
            p50_exclusion_reason = "Sin primera gestión humana válida acreditada."
        elif p50_temperature not in {"HOT", "NORMAL"}:
            p50_unverified_temperature += 1
            p50_exclusion_reason = p50_temperature_source or "Temperatura no verificable."
        else:
            p50_exclusion_reason = None
            (management_minutes_hot if p50_temperature == "HOT" else management_minutes_normal).append(minutes)

        temperature = _text(cycle.get("temperature_at_assignment")).upper()
        is_hot = temperature == "HOT"
        if is_hot:
            hot += 1
        elif temperature in {"NORMAL", "COLD"}:
            normal += 1
        else:
            unknown_temperature += 1

        sla_state = "insufficient_data"
        reason_not_evaluable = None
        hot_start = coerce_utc_datetime(cycle.get("hot_started_at"))
        if not assigned_at or temperature not in {"HOT", "NORMAL", "COLD"}:
            reason_not_evaluable = "Falta fecha de inicio SLA o temperatura inicial del episodio."
        elif hot_start and hot_start > assigned_at:
            # Product uses an escalated Hot segment; the approved managerial
            # definition starts Hot from episode assignment. Do not retro-apply.
            reason_not_evaluable = (
                "Escalación Normal→Hot: la política productiva y la definición gerencial difieren."
                if not is_hot else
                "El tramo Hot productivo comienza después de la asignación; la regla gerencial parte del episodio."
            )
        else:
            try:
                sla = calculate_sla(
                    assigned_at=assigned_at,
                    first_valid_management_at=first_at,
                    now=as_of,
                    temperature="HOT" if is_hot else "NORMAL",
                )
                if sla.get("coverage") == "insufficient_data":
                    reason_not_evaluable = "La fuente canónica no permite calcular el inicio del SLA."
                elif first_at:
                    sla_state = sla.get("canonical_state") or "insufficient_data"
                elif sla.get("canonical_state") == "BREACHED":
                    sla_state = "BREACHED"
                else:
                    reason_not_evaluable = "El plazo del SLA aún no se ha consumido al corte de consulta."
            except Exception:
                reason_not_evaluable = "No se pudo reproducir el cálculo canónico del SLA."

        if sla_state == "MANAGED_WITHIN_SLA":
            within_hot += int(is_hot)
            within_normal += int(not is_hot)
        elif sla_state == "MANAGED_OUTSIDE_SLA" or sla_state == "BREACHED":
            outside_hot += int(is_hot)
            outside_normal += int(not is_hot)
        else:
            insufficient += 1

        marker = coerce_utc_datetime(cycle.get("first_valid_management_at"))
        result_times = [coerce_utc_datetime(row.get("occurred_at")) for row in valid_episode_results]
        result_times = [value for value in result_times if value]
        first_result = min(result_times) if result_times else None
        latest_result_row = max(
            (row for row in valid_episode_results if coerce_utc_datetime(row.get("occurred_at"))),
            key=lambda row: coerce_utc_datetime(row.get("occurred_at")),
            default=None,
        )
        latest_result_at = coerce_utc_datetime((latest_result_row or {}).get("occurred_at"))
        if marker and first_result and marker != first_result:
            mismatch_count += 1
            if first_at == marker and first_source == "crm_events":
                mismatch_confirmed_by_event += 1

        management_traceable = bool(cycle_id and coerce_utc_datetime(cycle.get("assigned_at")))
        episode_rows.append({
            "lead_id": lead_id,
            "lead_code": _text(((lead_by_id.get(lead_id) or {}).get("prospecto") or {}).get("codigo")) or "Sin código",
            "commune": _text(((lead_by_id.get(lead_id) or {}).get("prospecto") or {}).get("comuna")) or "Sin comuna",
            "origin": _text(
                ((lead_by_id.get(lead_id) or {}).get("prospecto") or {}).get("origen")
                or (lead_by_id.get(lead_id) or {}).get("origin")
                or (lead_by_id.get(lead_id) or {}).get("lead_origin")
            ) or None,
            "cycle_id": cycle_id,
            "executive": _text(cycle.get("assigned_to_display_name")) or "Sin ejecutivo identificado",
            "assignment_type": assignment_kind,
            "assigned_at": coerce_utc_datetime(cycle.get("assigned_at")).isoformat() if coerce_utc_datetime(cycle.get("assigned_at")) else None,
            "sla_started_at": assigned_at.isoformat() if assigned_at else None,
            "temperature": "HOT" if is_hot else "NORMAL" if temperature in {"NORMAL", "COLD"} else "Sin clasificar",
            "first_management_at": first_at.isoformat() if first_at else None,
            "management_traceable": management_traceable,
            "first_management_source": first_source,
            "last_recorded_crm_result": normalize_result((latest_result_row or {}).get("result_type")),
            "last_recorded_crm_result_at": latest_result_at.isoformat() if latest_result_at else None,
            "current_crm_stage": _text(
                (lead_by_id.get(lead_id) or {}).get("pipeline_stage")
                or (lead_by_id.get(lead_id) or {}).get("stage")
                or (lead_by_id.get(lead_id) or {}).get("status")
            ).upper() or None,
            "first_management_business_minutes": round(minutes, 1) if minutes is not None else None,
            "management_before_sla_start": management_before_sla_start,
            "p50_temperature": p50_temperature,
            "p50_temperature_source": p50_temperature_source,
            "p50_exclusion_reason": p50_exclusion_reason,
            "contact_attempt": has_attempt,
            "effective_contact": has_effective_contact,
            "sla_state": sla_state,
            "sla_not_evaluable_reason": reason_not_evaluable,
        })

    unique_leads_sla_rescued = {
        _identity(row.get("lead_id")) for row in episode_rows
        if row.get("lead_id") and row.get("assignment_type") == "sla_rescue"
    }

    # Lead-level SLA cohort: count each lead once, using its first episode
    # assigned to this executive inside the selected window. Later reassignments
    # remain separate operational episodes and cannot duplicate or replace the
    # initial receipt outcome for this lead cohort.
    first_episode_by_lead: dict[str, dict[str, Any]] = {}
    for row in episode_rows:
        lead_key = _identity(row.get("lead_id"))
        if not lead_key:
            continue
        row_at = coerce_utc_datetime(row.get("assigned_at"))
        previous = first_episode_by_lead.get(lead_key)
        previous_at = coerce_utc_datetime((previous or {}).get("assigned_at"))
        if (previous is None or
                (row_at is not None and (previous_at is None or row_at < previous_at)) or
                (row_at == previous_at and _text(row.get("cycle_id")) < _text(previous.get("cycle_id")))):
            first_episode_by_lead[lead_key] = row

    lead_sla_within = lead_sla_outside = lead_sla_not_evaluable = 0
    lead_sla_segments = {
        "hot": {"within": 0, "outside": 0, "not_evaluable": 0},
        "normal": {"within": 0, "outside": 0, "not_evaluable": 0},
        "unknown": {"within": 0, "outside": 0, "not_evaluable": 0},
    }
    for row in first_episode_by_lead.values():
        state = row.get("sla_state")
        if state == "MANAGED_WITHIN_SLA":
            outcome = "within"
            lead_sla_within += 1
        elif state in {"MANAGED_OUTSIDE_SLA", "BREACHED"}:
            outcome = "outside"
            lead_sla_outside += 1
        else:
            outcome = "not_evaluable"
            lead_sla_not_evaluable += 1
        temperature_key = "hot" if row.get("temperature") == "HOT" else (
            "normal" if row.get("temperature") == "NORMAL" else "unknown"
        )
        lead_sla_segments[temperature_key][outcome] += 1

    lead_sla_evaluable = lead_sla_within + lead_sla_outside
    for segment in lead_sla_segments.values():
        segment["evaluable"] = segment["within"] + segment["outside"]

    # Keep the meeting-card management rate on the same unique-lead cohort as
    # the lead-level SLA card. Repeated assignment cycles remain available in
    # the operational episode detail, but cannot inflate this summary.
    lead_management_rows = list(first_episode_by_lead.values())
    lead_management_traceable_rows = [
        row for row in lead_management_rows if row.get("management_traceable")
    ]
    lead_management_managed = sum(
        1 for row in lead_management_traceable_rows if row.get("first_management_at")
    )
    lead_management_hot_minutes: list[float] = []
    lead_management_normal_minutes: list[float] = []
    lead_management_unverified_temperature = 0
    lead_management_unverified_time = 0
    lead_management_without_management = 0
    for row in lead_management_traceable_rows:
        if not row.get("first_management_at"):
            lead_management_without_management += 1
            continue
        minutes = row.get("first_management_business_minutes")
        if minutes is None:
            lead_management_unverified_time += 1
        elif row.get("p50_temperature") == "HOT":
            lead_management_hot_minutes.append(float(minutes))
        elif row.get("p50_temperature") == "NORMAL":
            lead_management_normal_minutes.append(float(minutes))
        else:
            lead_management_unverified_temperature += 1

    lead_management_denominator = len(lead_management_traceable_rows)
    lead_management_p50 = {
        "hot": {
            "business_minutes": _median(lead_management_hot_minutes),
            "n": len(lead_management_hot_minutes),
            "sla_reference_minutes": 60,
            "low_sample": len(lead_management_hot_minutes) < 10,
            "descriptive_only": len(lead_management_hot_minutes) < 20,
        },
        "normal": {
            "business_minutes": _median(lead_management_normal_minutes),
            "n": len(lead_management_normal_minutes),
            "sla_reference_minutes": 180,
            "low_sample": len(lead_management_normal_minutes) < 10,
            "descriptive_only": len(lead_management_normal_minutes) < 20,
        },
        "unverified_temperature_leads": lead_management_unverified_temperature,
        "unverified_time_leads": lead_management_unverified_time,
        "without_first_management_leads": lead_management_without_management,
        "reconciliation": (
            len(lead_management_hot_minutes) + len(lead_management_normal_minutes)
            + lead_management_unverified_temperature + lead_management_unverified_time
            + lead_management_without_management
        ),
    }

    # Losses on the meeting dashboard must be measured within the same received
    # cohort as the opportunities card. Count only confirmed SLA transfers
    # whose source cycle is one of this executive's episodes in the selected
    # period, and whose lead is part of that cohort. Otherwise the card can
    # show losses from leads received in earlier periods beside this period's
    # received-lead total, which looks like an impossible reconciliation.
    cohort_cycle_ids = {_text(row.get("cycle_id")) for row in episode_rows if row.get("cycle_id")}
    cohort_lead_ids = set(unique_leads)
    lost_by_sla = [
        audit for audit in unique_audits.values()
        if audit.get("event_type") == "SLA_REASSIGNMENT_COMMITTED"
        and _text(audit.get("source_cycle_id")) in cohort_cycle_ids
        and _identity(audit.get("lead_id")) in cohort_lead_ids
        and not _is_control_non_broker(
            audit.get("source_owner_display_name") or audit.get("source_owner_name")
        )
        and (not selected or _text(
            audit.get("source_owner_display_name") or audit.get("source_owner_name")
        ).casefold() == selected)
    ]
    unique_leads_lost_by_sla = {
        _identity(audit.get("lead_id")) for audit in lost_by_sla if audit.get("lead_id") is not None
    }
    evaluable = within_hot + outside_hot + within_normal + outside_normal
    return {
        "episodes_received": len(included),
        "unique_leads_received": len(unique_leads),
        "unique_leads_sla_rescued": len(unique_leads_sla_rescued),
        "assignment_types": {"original": original, "sla_rescue": rescue,
                              "other_transfer": other_transfer, "unknown": unknown_assignment},
        "temperature_mix": {"hot": hot, "normal": normal, "unknown": unknown_temperature},
        "sla": {
            "within": within_hot + within_normal,
            "outside": outside_hot + outside_normal,
            "evaluable": evaluable,
            "not_evaluable": insufficient,
            "hot": {"within": within_hot, "outside": outside_hot, "evaluable": within_hot + outside_hot},
            "normal": {"within": within_normal, "outside": outside_normal, "evaluable": within_normal + outside_normal},
        },
        "unique_lead_sla": {
            "within": lead_sla_within,
            "outside": lead_sla_outside,
            "evaluable": lead_sla_evaluable,
            "not_evaluable": lead_sla_not_evaluable,
            "leads_received": len(first_episode_by_lead),
            "hot": lead_sla_segments["hot"],
            "normal": lead_sla_segments["normal"],
            "unknown_temperature": lead_sla_segments["unknown"],
            "cohort_definition": "Un lead único, usando su primer episodio asignado al ejecutivo dentro del período; reasignaciones posteriores no duplican ni reemplazan el resultado.",
        },
        "first_management": {
            "episodes_managed": sum(1 for row in episode_rows if row.get("first_management_at") and row.get("management_traceable")),
            "denominator": sum(1 for row in episode_rows if row.get("management_traceable")),
            "traceable": sum(1 for row in episode_rows if row.get("management_traceable")),
            "untraceable": sum(1 for row in episode_rows if not row.get("management_traceable")),
            "coverage_pct": round((sum(1 for row in episode_rows if row.get("first_management_at") and row.get("management_traceable")) / sum(1 for row in episode_rows if row.get("management_traceable")) * 100), 1) if any(row.get("management_traceable") for row in episode_rows) else None,
            "p50_business_minutes": _median(management_minutes) if len(management_minutes) >= 5 else None,
            "p50_n": len(management_minutes), "including_late": True,
            "p50_by_temperature": {
                "hot": {
                    "business_minutes": _median(management_minutes_hot),
                    "n": len(management_minutes_hot),
                    "sla_reference_minutes": 60,
                    "low_sample": len(management_minutes_hot) < 10,
                    "descriptive_only": len(management_minutes_hot) < 20,
                },
                "normal": {
                    "business_minutes": _median(management_minutes_normal),
                    "n": len(management_minutes_normal),
                    "sla_reference_minutes": 180,
                    "low_sample": len(management_minutes_normal) < 10,
                    "descriptive_only": len(management_minutes_normal) < 20,
                },
                "unverified_temperature_episodes": p50_unverified_temperature,
                "unverified_time_episodes": p50_unverified_time,
                "without_first_management_episodes": p50_not_managed,
                "episode_reconciliation": (
                    len(management_minutes_hot) + len(management_minutes_normal)
                    + p50_unverified_temperature + p50_unverified_time + p50_not_managed
                ),
            },
            "observation_as_of": as_of.isoformat(),
            "unique_leads": {
                "managed": lead_management_managed,
                "denominator": lead_management_denominator,
                "traceable": lead_management_denominator,
                "untraceable": len(lead_management_rows) - lead_management_denominator,
                "leads_received": len(lead_management_rows),
                "coverage_pct": round(lead_management_managed / lead_management_denominator * 100, 1) if lead_management_denominator else None,
                "effective_contact_leads": sum(1 for row in lead_management_rows if row.get("effective_contact")),
                "attempt_leads": sum(1 for row in lead_management_rows if row.get("contact_attempt")),
                "p50_by_temperature": lead_management_p50,
                "cohort_definition": "Un lead único usando su primer episodio asignado al ejecutivo en el período; los episodios posteriores no duplican la primera gestión.",
            },
        },
        "contact_evidence": {"attempt_episodes": len(attempt_cycle_ids),
                             "effective_contact_episodes": len(effective_cycle_ids),
                             "basis": "crm_management_results completed with actor_user_id and canonical EFFECTIVE_CONTACT_RESULTS"},
        "sla_losses_executed": len(lost_by_sla),
        "unique_leads_lost_by_sla": len(unique_leads_lost_by_sla),
        "sla_audit_events": len(sla_audits),
        "reconciliation": {"marker_vs_result_discrepancies": mismatch_count, "confirmed_by_human_event": mismatch_confirmed_by_event},
        "duplicate_cycle_records_ignored": duplicate_cycle_records,
        "duplicate_sla_audit_records_ignored": duplicate_audit_records,
        "excluded_noncommercial_cycles": excluded_tests + excluded_technical,
        "excluded_test_cycles": excluded_tests,
        "excluded_technical_cycles": excluded_technical,
        "excluded_non_broker_cycles": excluded_non_broker,
        "records": sorted(episode_rows, key=lambda row: row.get("assigned_at") or ""),
        "availability": {
            "status": "available",
            "methodology_start": "2026-08-01",
            "population": "assignment_episodes_started_in_selected_month",
            "limitations": [
                "La temperatura solo se informa cuando está guardada en el episodio.",
                "Los episodios Normal→Hot no se comparan por SLA mientras difieran las reglas gerencial y productiva.",
                "Origen y operación no tienen una instantánea histórica por episodio.",
                "Visita realizada, oferta y cierre no cuentan con eventos verificables suficientes.",
                "La cartera histórica abierta al cierre no puede reconstruirse con el estado actual del lead.",
            ],
        },
    }


def unavailable_month_payload(reason: str, *, period_start: str, period_end: str) -> dict[str, Any]:
    return {
        "period": {"start": period_start, "end": period_end},
        "availability": {
            "status": "unavailable",
            "methodology_start": "2026-08-01",
            "reason": reason,
            "available_months": sorted(COVERED_MONTHS),
        },
        "indicators": {
            "visits": {"status": "unavailable"},
            "offers": {"status": "unavailable"},
            "closures": {"status": "unavailable"},
            "month_end_open_portfolio": {"status": "unavailable"},
            "property_inquiries_per_exposure_day": {"status": "unavailable"},
        },
    }
