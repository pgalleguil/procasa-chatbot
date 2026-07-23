"""Dry-run and optional migration to resolve duplicate HOT notifications.

This script:
1. Finds all groups with duplicate ``individual_identity`` in ``crm_notifications_v1``.
2. In dry-run mode (default): prints the groups, documents, and which would be kept.
3. In apply mode (``--apply``): marks duplicates with ``dedupe_active=false``,
   ``duplicate_of=<canonical_id>``, ``dedupe_resolution="historical_duplicate"``,
   preserving all original fields (provider_message_id, timestamps, attempts, etc.).

Usage:
    python scripts/resolve_hot_notification_duplicates.py           # dry-run
    python scripts/resolve_hot_notification_duplicates.py --apply   # mark duplicates
"""
from __future__ import annotations

import argparse
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("MONGO_URI", "mongodb+srv://pgalleguil:vLr5MTTZ7kcNzjSZ@cluster0.mzve39k.mongodb.net/?retryWrites=true&w=majority")
os.environ.setdefault("DB_NAME", "URLS")

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from datetime import datetime, timezone


def get_db():
    client = MongoClient(
        os.environ["MONGO_URI"],
        socketTimeoutMS=30000,
        connectTimeoutMS=10000,
        serverSelectionTimeoutMS=20000,
    )
    return client[os.environ["DB_NAME"]]


COLLECTION = "crm_notifications_v1"
INDIVIDUAL_IDENTITY_FIELD = "individual_identity"
NOTIFICATION_TYPE = "lead_assignment_hot"


def find_duplicates(db) -> list:
    """Return groups with >1 document sharing the same individual_identity."""
    pipeline = [
        {"$match": {"notification_type": NOTIFICATION_TYPE, INDIVIDUAL_IDENTITY_FIELD: {"$exists": True}}},
        {"$group": {
            "_id": f"${INDIVIDUAL_IDENTITY_FIELD}",
            "count": {"$sum": 1},
            "ids": {"$push": "$_id"},
            "states": {"$push": "$state"},
            "providers": {"$push": {"$ifNull": ["$provider_message_id", None]}},
            "created_ats": {"$push": "$created_at"},
        }},
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}},
    ]
    return list(db[COLLECTION].aggregate(pipeline))


def dry_run(db):
    dups = find_duplicates(db)
    if not dups:
        print("No duplicate groups found.")
        return

    print(f"Found {len(dups)} duplicate group(s):\n")
    for group in dups:
        print(f"  Identity: {group['_id'][:100]}...")
        print(f"  Count: {group['count']}")
        for i in range(group["count"]):
            marker = " ← CANONICAL (kept)" if i == 0 else " ← DUPLICATE (would be marked)"
            print(f"    [{i}] id={group['ids'][i]}  "
                  f"state={group['states'][i]}  "
                  f"provider={group['providers'][i]}  "
                  f"created={group['created_ats'][i]}{marker}")
        print()


def apply_migration(db):
    dups = find_duplicates(db)
    if not dups:
        print("No duplicate groups to resolve.")
        return

    now = datetime.now(timezone.utc)
    total_marked = 0

    for group in dups:
        # First document (oldest created_at) is kept as canonical.
        # The partial unique index requires `dedupe_active=True` on the canonical
        # document, so we must set it if not already present.
        canonical_id = group["ids"][0]
        db[COLLECTION].update_one(
            {"_id": canonical_id, "dedupe_active": {"$ne": True}},
            {"$set": {"dedupe_active": True}},
        )
        print(f"  Canonical: {canonical_id} — ensured dedupe_active=True")

        for i in range(1, group["count"]):
            dup_id = group["ids"][i]
            db[COLLECTION].update_one(
                {"_id": dup_id},
                {"$set": {
                    "dedupe_active": False,
                    "duplicate_of": canonical_id,
                    "dedupe_resolution": "historical_duplicate",
                    "dedupe_resolved_at": now,
                }},
            )
            print(f"  Marked:    {dup_id} → dedupe_active=False, duplicate_of={canonical_id}")
            total_marked += 1

    print(f"\nTotal documents marked as duplicates: {total_marked}")

    # Verify no remaining duplicates with dedupe_active=True
    remaining = find_duplicates_with_active(db)
    if remaining:
        print(f"\n⚠ WARNING: {len(remaining)} group(s) still have active duplicates!")
        print("Run the migration again after resolving these manually.")
    else:
        print("\n✓ All active duplicates resolved. The unique index can now be created.")


def find_duplicates_with_active(db):
    """Find duplicates where ALL documents still have dedupe_active=True."""
    pipeline = [
        {"$match": {"notification_type": NOTIFICATION_TYPE, INDIVIDUAL_IDENTITY_FIELD: {"$exists": True},
                     "dedupe_active": {"$ne": False}}},
        {"$group": {
            "_id": f"${INDIVIDUAL_IDENTITY_FIELD}",
            "count": {"$sum": 1},
            "ids": {"$push": "$_id"},
            "dedupe_active_values": {"$push": {"$ifNull": ["$dedupe_active", "missing"]}},
        }},
        {"$match": {"count": {"$gt": 1}}},
    ]
    return list(db[COLLECTION].aggregate(pipeline))


def check_index(db):
    """Check if the unique index exists."""
    indexes = list(db[COLLECTION].list_indexes())
    index_names = {idx["name"] for idx in indexes}
    target = "uq_crm_notification_individual_v1"
    if target in index_names:
        print(f"\n✓ Index '{target}' exists.")
    else:
        print(f"\n✗ Index '{target}' does NOT exist. Run with --create-index after --apply.")


def create_index(db):
    """Create the unique partial index after resolving duplicates."""
    from chatbot.crm_notifications import ensure_unique_indexes
    result = ensure_unique_indexes(db)
    if result.get("blocked"):
        print(f"\n✗ Cannot create index: {result['dry_run']}")
        print("Run with --apply first to resolve duplicates.")
    elif result.get("created"):
        print(f"\n✓ Index created: {result['created']}")
    else:
        print(f"\n? Unexpected result: {result}")


def main():
    parser = argparse.ArgumentParser(description="Resolve duplicate HOT notification records")
    parser.add_argument("--apply", action="store_true", help="Apply migration (mark duplicates)")
    parser.add_argument("--create-index", action="store_true", help="Create unique index after resolution")
    args = parser.parse_args()

    db = get_db()

    if args.create_index:
        check_index(db)
        create_index(db)
        check_index(db)
    elif args.apply:
        print("=== DRY-RUN FIRST ===\n")
        dry_run(db)
        print("\n=== APPLYING MIGRATION ===\n")
        apply_migration(db)
        print("\n=== INDEX CHECK ===")
        check_index(db)
    else:
        print("=== DRY-RUN (no changes) ===\n")
        dry_run(db)
        print("\nUse --apply to mark duplicates, --create-index to create the unique index.")


if __name__ == "__main__":
    main()
