"""CRM SLA Alert Report — read-only monitoring script.

Usage:
  python scripts/report_crm_sla_alerts.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chatbot.storage import get_async_db
from chatbot.crm_sla_alert_repository import COLLECTION
from chatbot.crm_sla_alert_templates import MESSAGE_DOMAIN


def mask_phone(p):
    if not p: return "<none>"
    d = "".join(c for c in p if c.isdigit())
    return f"+56 9 **** {d[-4:]}" if len(d) >= 4 else "<invalid>"


async def report():
    db = get_async_db()
    col = db[COLLECTION]

    # Counts by state
    states = await col.aggregate([
        {"$group": {"_id": "$state", "count": {"$sum": 1}}}
    ]).to_list(length=20)
    total = await col.count_documents({})

    # Counts by level/profile
    levels = await col.aggregate([
        {"$group": {"_id": {"level": "$alert_level", "profile": "$sla_profile"}, "count": {"$sum": 1}}}
    ]).to_list(length=20)

    # Outreach distribution
    outreach = await col.aggregate([
        {"$group": {"_id": "$outreach_state", "count": {"$sum": 1}}}
    ]).to_list(length=20)

    # Recent alerts (last 50)
    recent = await col.find({}).sort("created_at", -1).limit(50).to_list(length=50)

    print("=" * 72)
    print("CRM SLA ALERT — MONITORING REPORT (READ-ONLY)")
    print(f"  Collection: {COLLECTION}  |  Domain: {MESSAGE_DOMAIN}")
    print(f"  Total documents: {total}")
    print("=" * 72)

    print("\nBY STATE:")
    for s in sorted(states, key=lambda x: -x["count"]):
        print(f"  {s['_id']:25s} {s['count']:>4}")

    print("\nBY LEVEL/PROFILE:")
    for l in sorted(levels, key=lambda x: -x["count"]):
        k = l["_id"]
        print(f"  {k.get('profile','?'):10s} {k.get('level','?'):10s} {l['count']:>4}")

    print("\nBY OUTREACH:")
    for o in sorted(outreach, key=lambda x: -x["count"]):
        print(f"  {o['_id']:25s} {o['count']:>4}")

    if recent:
        print(f"\nRECENT ({len(recent)} most recent):")
        for r in recent[:10]:
            lid = str(r.get("lead_id", ""))[:10]
            exec_name = "?"
            if r.get("lead_id"):
                lead = await db["leads"].find_one({"_id": r["lead_id"]}, {"ejecutivo_asignado": 1})
                exec_name = lead.get("ejecutivo_asignado", "?") if lead else "?"

            print(f"  {r.get('created_at')} | {r.get('alert_level'):8s} | "
                  f"{r.get('sla_profile'):8s} | {r.get('state'):15s} | "
                  f"outreach={r.get('outreach_state','?'):18s} | "
                  f"exec={exec_name:20s} | lead={lid}... | "
                  f"msg_id={r.get('provider_message_id','-')}")

    print("\n" + "=" * 72)
    print("END OF REPORT.  Writes: 0  |  Provider calls: 0")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(report())
