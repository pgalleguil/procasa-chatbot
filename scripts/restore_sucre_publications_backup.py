#!/usr/bin/env python
"""Restore only publication subdocuments from a verified Sucre backup."""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

from bson import json_util

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chatbot.publication_sync import (
    COLLECTION_NAME,
    _active_mongo_property,
    _ensure_backups,
    _office_consistency,
    _strict_update_filter,
)
from scraping_convecta.scraping_prop360_ficha_completa import get_mongo_collection


def _read_backup(path: Path) -> list[dict]:
    raw = gzip.open(path, "rb").read() if path.suffix.lower() == ".gz" else path.read_bytes()
    docs = json_util.loads(raw.decode("utf-8"))
    if not isinstance(docs, list) or any(not isinstance(doc, dict) for doc in docs):
        raise ValueError("backup_must_be_a_json_array_of_documents")
    if any("_id" not in doc or "codigo" not in doc or "publicaciones" not in doc for doc in docs):
        raise ValueError("backup_missing_required_identity_or_publications")
    codes = [str(doc["codigo"]).strip() for doc in docs]
    if len(codes) != len(set(codes)):
        raise ValueError("backup_has_duplicate_property_codes")
    return docs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run-first restoration of only `publicaciones` from backup.")
    parser.add_argument("backup", type=Path)
    parser.add_argument("--office-id", type=int, default=None)
    parser.add_argument("--execute", action="store_true", help="Write the backed-up publicaciones subdocuments.")
    parser.add_argument("--safety-backup-dir", default=None,
                        help="Required for execution; stores current state before restoration.")
    args = parser.parse_args(argv)
    if args.office_id != 7:
        print("ERROR: --office-id 7 is required.", file=sys.stderr)
        return 2
    if args.execute and not args.safety_backup_dir:
        print("ERROR: --execute requires --safety-backup-dir.", file=sys.stderr)
        return 2
    client = None
    try:
        source_docs = _read_backup(args.backup)
        client, coll = get_mongo_collection(COLLECTION_NAME)
        candidates, missing, rejected = [], [], []
        for prior in source_docs:
            current = coll.find_one({"_id": prior["_id"]})
            if not current:
                missing.append(str(prior["codigo"]))
                continue
            in_sucre, reason = _office_consistency(current)
            if not in_sucre or not _active_mongo_property(current):
                rejected.append({"codigo": str(prior["codigo"]), "reason": reason or "not_active_sucre"})
                continue
            candidates.append((prior, current))
        if args.execute and (missing or rejected or len(candidates) != len(source_docs)):
            raise RuntimeError("restore_scope_preflight_failed; no documents were restored")
        safety = None
        if args.execute:
            safety = _ensure_backups([current for _prior, current in candidates],
                                     "restore_preflight", args.safety_backup_dir)
            if safety.get("documents") != len(candidates):
                raise RuntimeError("restore_safety_backup_verification_failed")
        written = 0
        if args.execute:
            for prior, current in candidates:
                old_publications = current.get("publicaciones") or {}
                target_publications = prior.get("publicaciones") or {}
                if old_publications == target_publications:
                    continue
                result = coll.update_one(
                    _strict_update_filter(current),
                    {"$set": {"publicaciones": target_publications}},
                )
                if getattr(result, "modified_count", 0) != 1:
                    raise RuntimeError(f"restore_compare_and_swap_failed:{prior['codigo']}")
                written += 1
        print(json.dumps({
            "office_id": 7, "dry_run": not args.execute, "backup_path": str(args.backup),
            "backup_documents": len(source_docs), "eligible": len(candidates),
            "missing": missing, "scope_rejected": rejected,
            "safety_backup": safety, "documents_restored": written,
            "fields_written": ["publicaciones"] if args.execute else [],
        }, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"RESTORE_FAILED={type(exc).__name__}:{str(exc)[:250]}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
