"""Automatic price-drop batches from the office computer, without per-lead approval.

DRY RUN (no writes/sends):
    python scripts/enviar_rebajas_automatico.py
SEND UP TO THE OPERATOR-SELECTED LIMIT OF NEW LEADS:
    python scripts/enviar_rebajas_automatico.py --execute --max-messages 20 --lead-max-age-days 0

Only leads with a prior inquiry about the same property before its verified
price reduction are eligible. Separate WhatsApp opt-in reviews and per-message
approvals are not required. STOP, revocation, completed-search, duplicate and
delivery protections remain active. No executive sends.
This runs ONCE, when invoked; it is not a scheduled/background campaign.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if os.getenv("PRICE_DROP_ENV_FILE"):
    from dotenv import load_dotenv
    load_dotenv(Path(os.environ["PRICE_DROP_ENV_FILE"]).expanduser(), override=False)
sys.path.insert(0, str(ROOT))

from chatbot import price_drop_leads_v2 as v2
from chatbot.price_drop_campaign_classifier import lead_must_not_be_contacted
from chatbot.price_drop_campaign_discovery import find_candidates_for_event
from scripts import enviar_piloto_rebajas as cli

AUTOMATIC_OPERATOR = "PROCASA_AUTOMATIC_BATCH"
BLOCKING_CAMPAIGN_STATES = {
    "PREPARED", "APPROVED", "SENDING", "DELIVERY_UNKNOWN",
}
CONTACT_COOLDOWN_STATES = {
    "SENT", "RESPONDED", "DECLINED", "OPTED_OUT",
}


def recent_outreach_or_pending(lead, *, now, cooldown_days=30):
    """Prevent competing campaigns and repeated WhatsApp outreach."""
    cutoff = now - timedelta(days=cooldown_days)
    for campaign in (lead.get("price_drop_v2") or {}).get("campaigns") or []:
        if not isinstance(campaign, dict):
            continue
        status = str(campaign.get("status") or "").upper()
        if status in BLOCKING_CAMPAIGN_STATES:
            return True
        if status in CONTACT_COOLDOWN_STATES:
            stamp = cli._campaign_stamp(campaign)
            # Unknown historical send timestamp fails closed, not open.
            if stamp is None or stamp >= cutoff:
                return True
    return False


def choose_new_recipients(db, *, max_messages, max_age_days, now,
                          property_code=None, max_events=None,
                          progress: Callable[[str], None] | None = None):
    """Only leads with a verified pre-reduction inquiry about this property.

    Share the read-only audit's one-time raw-phone ownership index during
    selection. The actual send path still checks phone uniqueness against
    MongoDB immediately before sending each message.
    """
    if progress:
        progress("discovering_price_drops")
    events = cli._events(
        db, property_code=property_code, limit=max_events, progress=progress,
    )
    events = sorted(events, key=lambda event: (event.dropped_at, event.event_id), reverse=True)
    if not events:
        if progress:
            progress("selection_completed_recipients_0")
        return [], {"events_examined": 0, "events_discovered": 0}
    if progress:
        progress(f"loading_phone_index_events_{len(events)}")
    phone_owner_index: dict[str, set[str]] = {}
    cursor = db["leads"].find(
        {}, {"_id": 1, "phone": 1, "contact_phone_normalized": 1},
    ).batch_size(200)
    for row in cursor:
        lead_id = str(row["_id"])
        for field in ("phone", "contact_phone_normalized"):
            value = row.get(field)
            values = value if isinstance(value, list) else [value]
            for raw_phone in values:
                if isinstance(raw_phone, str) and raw_phone:
                    phone_owner_index.setdefault(raw_phone, set()).add(lead_id)
    chosen, seen = [], set()
    event_count = 0
    for event in events:
        event_count += 1
        if progress:
            progress(f"matching_event_{event_count}_of_{len(events)}_property_{event.property_code}")
        candidates = find_candidates_for_event(
            db, event, temperature_filter="ALL", lead_max_age_days=max_age_days,
            now=now, phone_owner_index=phone_owner_index,
        )
        if progress:
            progress(f"matched_event_{event_count}_candidates_{len(candidates)}")
        for candidate in candidates:
            if not candidate.get("eligible"):
                continue
            identity = str(candidate["lead_id"])
            if identity in seen:
                continue
            lead = db["leads"].find_one({"_id": cli._lead_id(identity)})
            if not lead or lead_must_not_be_contacted(lead):
                continue
            # Explicit revocation/STOP remains binding even without an opt-in
            # prerequisite. Exclude before writing a PREPARED campaign.
            if v2.whatsapp_contact_authorization_blocker(lead, {}) is not None:
                continue
            phone = str(lead.get("contact_phone_normalized") or lead.get("phone") or "")
            if recent_outreach_or_pending(lead, now=now):
                continue
            # Preserve exactly one candidate per person across all properties.
            seen.add(identity)
            chosen.append({"event": event, "candidate": candidate,
                           "lead_id": identity, "last4": phone[-4:]})
            if len(chosen) >= max_messages:
                break
        if len(chosen) >= max_messages:
            break
    if progress:
        progress(f"selection_completed_recipients_{len(chosen)}")
    return chosen, {"events_examined": event_count,
                    "events_discovered": len(events)}


def verify_publications(db, chosen, *, now):
    """Fail before any write unless all advertised prices/links match the web."""
    results = {}
    for row in chosen:
        event = row["event"]
        if event.event_id in results:
            continue
        verification = cli._publication_result_dict(cli._publication_for_operation(
            cli.verify_current_listing, db,
            operation=event.operation.lower(), property_code=event.property_code,
            currency=event.currency, expected_price=event.current_price,
            expected_url=event.listing_url, now=now,
        ))
        results[event.event_id] = verification
    return results


def run(db, *, execute=False, max_messages=20, max_age_days=0,
        property_code=None, max_events=None, now=None,
        provider_plan="trial", min_delay=60.0, max_delay=90.0,
        daily_provider_cap=50,
        progress: Callable[[str], None] | None = None):
    if not 1 <= max_messages <= cli.MAX_MESSAGES_PER_RUN:
        raise ValueError("max_messages_must_be_within_provider_run_limit")
    cli.validate_limits(max_messages, min_delay, max_delay,
                        provider_plan=provider_plan, daily_cap=daily_provider_cap)
    stamp = now or datetime.now(timezone.utc)
    selected, counters = choose_new_recipients(
        db, max_messages=max_messages, max_age_days=max_age_days,
        now=stamp, property_code=property_code, max_events=max_events,
        progress=progress,
    )
    preview = [{
        "code": item["event"].property_code,
        "lead_last6": item["lead_id"][-6:],
        "phone_last4": item["last4"],
        "event_id": item["event"].event_id,
        "contact_basis": "VERIFIED_PRE_REDUCTION_INQUIRY",
    } for item in selected]
    report = {"mode": "EXECUTE" if execute else "DRY_RUN",
              "status": "CANDIDATES_SELECTED" if selected else "NO_ELIGIBLE_RECIPIENTS",
              "selected": len(selected), "requested_limit": max_messages,
              "required": max_messages,  # legacy report key, not an exact-batch requirement
              "recipients": preview, **counters,
              "approval": "AUTOMATIC_AFTER_PROPERTY_INQUIRY_VERIFICATION",
              "manual_approval_per_message": False, "attempts": 0}
    if not execute:
        return report
    if not selected:
        return report  # A smaller eligible audience is not a blocked batch.
    if os.getenv("RENDER", "").lower() == "true" or os.getenv("RENDER_SERVICE_ID"):
        return {**report, "status": "BLOCKED_MANUAL_CLI_ON_RENDER"}
    if os.getenv(v2.SEND_FLAG, "").lower() not in {"1", "true", "yes"}:
        return {**report, "status": "BLOCKED_SEND_RUNTIME_GATE"}
    if os.getenv(v2.CONTEXT_FLAG, "").lower() not in {"1", "true", "yes"}:
        return {**report, "status": "BLOCKED_CONTEXT_RUNTIME_GATE"}

    if progress:
        progress("verifying_publications_before_send")
    checks = verify_publications(db, selected, now=stamp)
    # A stale or unpublished property must not block other verified recipients.
    excluded = [{"code": row["event"].property_code,
                 "lead_last6": row["lead_id"][-6:],
                 "reason": str((checks.get(row["event"].event_id) or {}).get("status")
                               or "publication_not_verified")}
                for row in selected
                if not (checks.get(row["event"].event_id) or {}).get("sendable")]
    selected = [row for row in selected
                if (checks.get(row["event"].event_id) or {}).get("sendable")]
    if not selected:
        return {**report, "status": "BLOCKED_PUBLICATION_VERIFICATION",
                "publication_exclusions": excluded,
                "publication_checks": checks}

    # This preserves the existing backups and V2 prepare/approve audit ledger.
    if progress:
        progress("creating_pre_send_backup")
    backup = cli._backup_leads(db, [cli._lead_id(row["lead_id"]) for row in selected])
    batches = {}
    if progress:
        progress("preparing_and_approving_campaigns")
    for item in selected:
        event = item["event"]
        cli.prepare_selected(
            db, event=event, lead_ids=[item["lead_id"]],
            operator=AUTOMATIC_OPERATOR, contact_basis_reviewed=True,
            whatsapp_contact_reviewed=False, whatsapp_contact_evidence=None,
            temperature_filter="ALL", lead_max_age_days=max_age_days, now=stamp,
        )
        # Prior-property inquiry, STOP and duplicate-send protections are
        # rechecked before actual delivery; no human per-message approval.
        v2.approve_one(db, lead_id=item["lead_id"],
                       campaign_id=event.campaign_id, operator=AUTOMATIC_OPERATOR)
        batches.setdefault(event.campaign_id, set()).add(item["lead_id"])

    if progress:
        progress("executing_approved_batch")
    outcome = cli.execute_approved_batch(
        db, campaigns=batches, include_pilot=False,
        max_messages=max_messages, min_delay_seconds=min_delay,
        max_delay_seconds=max_delay, provider_plan=provider_plan,
        daily_provider_cap=daily_provider_cap,
        temperature_filter="ALL", lead_max_age_days=max_age_days,
        enforce_campaign_filters=True, already_confirmed=True,
    )
    return {**report, "status": outcome.get("status"),
            "sending_selected": len(selected),
            "publication_exclusions": excluded,
            "attempts": outcome.get("attempts", 0),
            "batch_result": outcome, "backup": str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-messages", type=int, default=20)
    parser.add_argument("--lead-max-age-days", type=int, default=0,
                        help="0 = sin restriccion de antiguedad")
    parser.add_argument("--property")
    parser.add_argument("--max-events", type=int, default=None,
                        help="opcional; sin valor se revisa todo el historial")
    parser.add_argument("--daily-provider-cap", type=int, default=50)
    parser.add_argument("--provider-plan", choices=("trial", "paid"), default="trial")
    parser.add_argument("--min-delay-seconds", type=float, default=60)
    parser.add_argument("--max-delay-seconds", type=float, default=90)
    args = parser.parse_args()
    if args.lead_max_age_days < 0 or (args.max_events is not None and args.max_events < 1):
        parser.error("lead age must be non-negative and optional event limit positive")
    try:
        outcome = run(
            cli._get_db(), execute=args.execute, max_messages=args.max_messages,
            max_age_days=args.lead_max_age_days, property_code=args.property,
            max_events=args.max_events, provider_plan=args.provider_plan,
            min_delay=args.min_delay_seconds, max_delay=args.max_delay_seconds,
            daily_provider_cap=args.daily_provider_cap,
        )
    except (ValueError, cli.CliError, v2.PilotBlocked) as exc:
        outcome = {"status": "BLOCKED", "reason": str(exc)[:180], "attempts": 0}
    print(json.dumps(outcome, default=str, ensure_ascii=False, indent=2))
    return 0 if outcome.get("status") not in {"BLOCKED", "ERROR"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
