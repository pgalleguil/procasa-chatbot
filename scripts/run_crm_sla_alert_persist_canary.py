"""CRM SLA Alert Persist Canary — controlled persistence to crm_sla_alerts_v1.

Usage:
  python scripts/run_crm_sla_alert_persist_canary.py --check
  python scripts/run_crm_sla_alert_persist_canary.py --ensure-indexes
  python scripts/run_crm_sla_alert_persist_canary.py --persist
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatbot.crm_sla_alert_pipeline import run_evaluation_and_persist_once
from chatbot.crm_sla_alert_settings import (
    CRM_SLA_ALERT_CUTOVER_RAW,
    CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS,
    validate_persist_config,
    validate_check_config,
    validate_indexes_config,
)


def print_sep(c="="):
    print(c * 72)


def anonymize_phone(p):
    if not p: return "<sin telefono>"
    d = "".join(c for c in p if c.isdigit())
    return f"+56 9 **** {d[-4:]}" if len(d) >= 4 else "<invalido>"


def check():
    config = validate_check_config()
    if not config["valid"]:
        print(f"CONFIG BLOCKED: {config['reason']}")
        return

    print_sep()
    print("CRM SLA ALERT — CHECK (READ-ONLY)")
    print(f"  Cutover              : {CRM_SLA_ALERT_CUTOVER_RAW}")
    print(f"  Allowlist user IDs   : {len(CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS)}")
    for uid in sorted(CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS):
        print(f"    - {uid}")
    print(f"  Config valid         : YES")
    print_sep()

    # Use evaluator directly for check (read-only, no persist config needed)
    from chatbot.crm_sla_alert_evaluator import evaluate_sla_alerts
    from chatbot.crm_sla_alert_settings import CRM_SLA_ALERT_CUTOVER_AT
    from chatbot.storage import get_async_db
    db = get_async_db()
    report = asyncio.run(evaluate_sla_alerts(db=db, alert_cutover=CRM_SLA_ALERT_CUTOVER_AT, limit_cycles=200))

    alerts = report.get("alerts", [])
    allowlist = CRM_SLA_ALERTS_CANARY_RECIPIENT_USER_IDS
    authorized = [a for a in alerts if a.get("recipient_user_id") in allowlist]

    print(f"  Candidates evaluated : {len(alerts)}")
    print(f"  Authorized (canary)  : {len(authorized)}")
    print(f"  Writes               : 0 (check only)")
    print(f"  Provider calls       : 0")
    print(f"  Reassignments        : 0")
    print_sep()


async def do_ensure_indexes():
    from chatbot.crm_sla_alert_settings import validate_indexes_config
    config = validate_indexes_config()
    if not config["valid"]:
        print(f"INDEXES BLOCKED: {config['reason']}")
        return
    from chatbot.crm_sla_alert_repository import ensure_crm_sla_alert_indexes
    from chatbot.storage import get_async_db
    db = get_async_db()
    await ensure_crm_sla_alert_indexes(db)
    print("Indexes ensured on crm_sla_alerts_v1.")


async def do_persist():
    config = validate_persist_config()
    if not config["valid"]:
        print(f"CONFIG BLOCKED: {config['reason']}")
        return

    report = await run_evaluation_and_persist_once(ensure_indexes=False, max_cycles=200)

    print_sep()
    print("CRM SLA ALERT — PERSIST CANARY")
    print(f"  Status               : {report.get('status')}")
    print(f"  Candidates evaluated : {report['candidates_evaluated']}")
    print(f"  Authorized           : {report['authorized']}")
    print(f"  Persisted            : {report['persisted']}")
    print(f"  Already exists       : {report['already_exists']}")
    print(f"  Excluded allowlist   : {report['excluded_by_allowlist']}")
    print(f"  Excluded no phone    : {report['excluded_no_phone']}")
    print(f"  Excluded by limit    : {report['excluded_by_limit']}")
    print(f"  Total writes         : {report['writes']}")
    print(f"  Provider calls       : {report['provider_calls']}")
    print_sep()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--ensure-indexes", action="store_true")
    g.add_argument("--persist", action="store_true")
    args = p.parse_args()

    if args.check:
        check()
    elif args.ensure_indexes:
        asyncio.run(do_ensure_indexes())
    elif args.persist:
        asyncio.run(do_persist())
