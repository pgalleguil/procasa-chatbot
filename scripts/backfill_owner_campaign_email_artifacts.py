"""Read-only Gmail Sent recovery and idempotent gzip artifact backfill.

Run from the application's configured runtime:
  python scripts/backfill_owner_campaign_email_artifacts.py --dry-run
  python scripts/backfill_owner_campaign_email_artifacts.py --execute

The dry-run stores only a gzip-compressed temporary staging cache. Mongo writes
are restricted to owner_campaign_email_artifacts.
"""

from __future__ import annotations

import argparse
import email
import gzip
import hashlib
import imaplib
import json
import re
import sys
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any

from bson.binary import Binary
from pymongo import MongoClient

from campanas.owner_campaign_live_config import PRODUCTION_CAMPAIGN_ID
from campanas.owner_campaign_live_config import WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID
from config import Config


EXPECTED_ARTIFACTS = 358
CAMPAIGN_IDS = (PRODUCTION_CAMPAIGN_ID, WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID)
EMAIL_ARTIFACT_COLLECTION = "owner_campaign_email_artifacts"
CACHE_PATH = Path("/tmp/owner_campaign_email_artifacts_backfill.jsonl.gz")
EXPECTED_SUBJECT_BASE = "Seguimiento comercial PROCASA"
EXPECTED_SUBJECT_OFFICE = f"{EXPECTED_SUBJECT_BASE} · PROCASA SUCRE · {{code}}"
_GMAIL_ID_RE = re.compile(rb"X-GM-MSGID\s+(\d+)", re.IGNORECASE)
_INTERNAL_DATE_RE = re.compile(rb'INTERNALDATE\s+"([^"]+)"', re.IGNORECASE)


def _attempt_for(row: dict[str, Any]) -> dict[str, Any]:
    attempts = row.get("send_attempts") if isinstance(row.get("send_attempts"), list) else []
    status = str(row.get("send_status") or "").upper()
    attempt_id = str(row.get("last_send_attempt_id") or "")
    matched = [a for a in attempts if isinstance(a, dict)
               and str(a.get("smtp_status") or "").upper() == status
               and (not attempt_id or str(a.get("attempt_id") or "") == attempt_id)]
    if not matched and status == "DELIVERY_UNKNOWN":
        matched = [a for a in attempts if isinstance(a, dict)
                   and str(a.get("smtp_status") or "").upper() == status]
    if not matched:
        raise ValueError(f"missing matching send attempt for {row.get('property_code')}")
    attempt = matched[-1]
    if not str(attempt.get("attempt_id") or "").strip():
        raise ValueError(f"missing attempt id for {row.get('property_code')}")
    message_id = str(attempt.get("message_id") or row.get("last_send_message_id") or "").strip()
    return {**attempt, "attempt_id": str(attempt["attempt_id"]), "message_id": message_id}


def _sent_folder(imap: imaplib.IMAP4_SSL) -> str:
    status, entries = imap.list()
    if status != "OK" or not entries:
        raise RuntimeError("unable to list IMAP folders")
    for entry in entries:
        if not entry or not re.search(rb"\\Sent(?:\s|\))", entry, re.I):
            continue
        match = re.search(rb'\s("(?:[^"\\]|\\.)*"|[^ ]+)\s*$', entry)
        if not match:
            continue
        mailbox = match.group(1).strip()
        if mailbox.startswith(b'"') and mailbox.endswith(b'"'):
            mailbox = re.sub(rb"\\([\\\"])", rb"\1", mailbox[1:-1])
        return mailbox.decode("utf-8", "strict")
    raise RuntimeError("Gmail Sent folder not found")


def _decoded_header(value: str | None) -> str:
    return str(make_header(decode_header(value or ""))).strip()


def _html_body(message: email.message.Message) -> str:
    candidates = message.walk() if message.is_multipart() else [message]
    for part in candidates:
        if part.get_content_type().lower() != "text/html":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="strict")
        except (LookupError, UnicodeDecodeError) as exc:
            raise ValueError("email HTML charset decode failed") from exc
        if text:
            return text
    raise ValueError("text/html part missing")


def _check_message(message: email.message.Message, row: dict[str, Any], attempt: dict[str, Any], uid_data: bytes) -> tuple[str, str, str, datetime]:
    code = str(row.get("property_code") or "").strip()
    owner = str(row.get("owner_email") or "").strip().casefold()
    mid = str(message.get("Message-ID") or "").strip()
    if attempt["message_id"] and mid.casefold() != attempt["message_id"].casefold():
        raise ValueError(f"RFC Message-ID mismatch for {code}")
    if not mid:
        raise ValueError(f"RFC Message-ID missing for {code}")
    recipients = {address.strip().casefold() for _, address in getaddresses([message.get("To", "")]) if address}
    if owner not in recipients:
        raise ValueError(f"To recipient mismatch for {code}")
    subject = _decoded_header(message.get("Subject"))
    valid_subjects = {EXPECTED_SUBJECT_BASE, EXPECTED_SUBJECT_OFFICE.format(code=code)}
    if subject not in valid_subjects:
        raise ValueError(f"subject identity mismatch for {code}")
    sent_header = message.get("Date")
    sent_at = parsedate_to_datetime(sent_header) if sent_header else None
    if sent_at is None:
        internal = _INTERNAL_DATE_RE.search(uid_data)
        if not internal:
            raise ValueError(f"sent timestamp missing for {code}")
        sent_at = parsedate_to_datetime(internal.group(1).decode("ascii"))
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    ledger_time = attempt.get("sent_at") or row.get("sent_at") or attempt.get("attempted_at")
    if isinstance(ledger_time, datetime):
        if ledger_time.tzinfo is None:
            ledger_time = ledger_time.replace(tzinfo=timezone.utc)
        if abs((sent_at.astimezone(timezone.utc) - ledger_time.astimezone(timezone.utc)).total_seconds()) > 48 * 3600:
            raise ValueError(f"sent timestamp mismatch for {code}")
    html = _html_body(message)
    raw = html.encode("utf-8", errors="strict")
    gmail_match = _GMAIL_ID_RE.search(uid_data)
    gmail_message_id = gmail_match.group(1).decode("ascii") if gmail_match else ""
    return html, mid, gmail_message_id, sent_at.astimezone(timezone.utc)


def _source_rows(ledger: Any, campaign_ids: tuple[str, ...]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    projection = {"campaign_id": 1, "property_code": 1, "owner_email": 1, "send_status": 1,
                  "sent_at": 1, "last_send_attempt_id": 1, "last_send_message_id": 1,
                  "send_attempts": 1}
    rows = []
    for campaign_id in campaign_ids:
        for status in ("SENT", "DELIVERY_UNKNOWN"):
            rows.extend(ledger.find({"campaign_id": campaign_id, "send_status": status}, projection))
    rows.sort(key=lambda row: (str(row.get("campaign_id")), str(row.get("property_code"))))
    if len(rows) != EXPECTED_ARTIFACTS:
        raise ValueError(f"expected {EXPECTED_ARTIFACTS} SENT/DELIVERY_UNKNOWN rows; found {len(rows)}")
    keys = [(str(r.get("campaign_id")), str(r.get("property_code"))) for r in rows]
    if len(set(keys)) != EXPECTED_ARTIFACTS:
        raise ValueError("duplicate campaign/property ledger identity")
    return [(row, _attempt_for(row)) for row in rows]


def _extract_all(rows: list[tuple[dict[str, Any], dict[str, Any]]], user: str, password: str) -> list[dict[str, Any]]:
    conn = imaplib.IMAP4_SSL("imap.gmail.com", timeout=45)
    documents: list[dict[str, Any]] = []
    try:
        conn.login(user, password)
        folder = _sent_folder(conn)
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError("unable to select Gmail Sent read-only")
        for index, (row, attempt) in enumerate(rows, start=1):
            print(f"GMAIL_READ={index}/{len(rows)} property_code={row.get('property_code')}", flush=True)
            if attempt["message_id"]:
                query_mid = attempt["message_id"].replace("\\", "\\\\").replace('"', '\\"')
                status, result = conn.uid("SEARCH", None, "HEADER", "Message-ID", f'"{query_mid}"')
                uids = result[0].split() if status == "OK" and result and result[0] else []
            else:
                # Delivery-unknown attempts may not have persisted Message-ID.
                # Resolve only by owner, property-specific subject, and the
                # recorded attempt date, then require exactly one exact match.
                attempted = attempt.get("attempted_at") or row.get("sent_at")
                if not isinstance(attempted, datetime):
                    raise ValueError(f"attempt timestamp missing for {row['property_code']}")
                if attempted.tzinfo is None:
                    attempted = attempted.replace(tzinfo=timezone.utc)
                from_day = (attempted - __import__("datetime").timedelta(days=2)).strftime("%d-%b-%Y")
                to_day = (attempted + __import__("datetime").timedelta(days=3)).strftime("%d-%b-%Y")
                status, result = conn.uid(
                    "SEARCH", None, "SINCE", from_day, "BEFORE", to_day,
                    "HEADER", "To", str(row["owner_email"]),
                )
                candidates = result[0].split() if status == "OK" and result and result[0] else []
                uids = []
                for candidate in candidates:
                    hs, header_fetch = conn.uid("FETCH", candidate, "(BODY.PEEK[HEADER] X-GM-MSGID INTERNALDATE)")
                    header_raw = next((entry[1] for entry in header_fetch if isinstance(entry, tuple) and isinstance(entry[1], bytes)), None)
                    if hs != "OK" or not header_raw:
                        continue
                    header_message = email.message_from_bytes(header_raw)
                    try:
                        _html_body  # keep MIME helper loaded without parsing the body
                        _check_message_header = _decoded_header(header_message.get("Subject"))
                        hdate = parsedate_to_datetime(header_message.get("Date"))
                        if hdate.tzinfo is None:
                            hdate = hdate.replace(tzinfo=timezone.utc)
                        to_set = {addr.strip().casefold() for _, addr in getaddresses([header_message.get("To", "")]) if addr}
                        if (
                            str(row["owner_email"]).strip().casefold() in to_set
                            and _check_message_header in {
                                EXPECTED_SUBJECT_BASE,
                                EXPECTED_SUBJECT_OFFICE.format(code=str(row["property_code"])),
                            }
                            and abs((hdate.astimezone(timezone.utc) - attempted.astimezone(timezone.utc)).total_seconds()) <= 48 * 3600
                        ):
                            uids.append(candidate)
                    except (TypeError, ValueError):
                        continue
                if len(uids) != 1:
                    raise ValueError(f"Gmail Sent identity not unique for {row['property_code']}")
            if status != "OK" or len(uids) != 1:
                raise ValueError(f"Gmail Sent source missing or ambiguous for {row['property_code']}")
            status, fetched = conn.uid("FETCH", uids[0], "(BODY.PEEK[] X-GM-MSGID INTERNALDATE)")
            if status != "OK" or not fetched:
                raise ValueError(f"Gmail Sent fetch failed for {row['property_code']}")
            raw_message = next((entry[1] for entry in fetched if isinstance(entry, tuple) and isinstance(entry[1], bytes)), None)
            if not raw_message:
                raise ValueError(f"Gmail MIME source missing for {row['property_code']}")
            message = email.message_from_bytes(raw_message)
            html, rfc_message_id, gmail_id, sent_at = _check_message(message, row, attempt, b" ".join(entry[0] for entry in fetched if isinstance(entry, tuple)))
            html_bytes = html.encode("utf-8")
            compressed = gzip.compress(html_bytes, mtime=0)
            document = {
                "_id": f"{row['campaign_id']}:{row['property_code']}",
                "campaign_id": row["campaign_id"],
                "property_code": str(row["property_code"]),
                "owner_email": str(row["owner_email"]).strip(),
                "message_id": rfc_message_id,
                "send_attempt_id": attempt["attempt_id"],
                "gmail_message_id": gmail_id,
                "sent_at": sent_at,
                "html_gzip": Binary(compressed),
                "html_encoding": "utf-8",
                "html_compression": "gzip",
                "original_html_sha256": hashlib.sha256(html_bytes).hexdigest(),
                "original_html_bytes": len(html_bytes),
                "compressed_bytes": len(compressed),
                "email_snapshot_source": "GMAIL_SENT",
                "created_at": datetime.now(timezone.utc),
            }
            documents.append(document)
        return documents
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _write_cache(documents: list[dict[str, Any]], path: Path) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as stream:
        for item in documents:
            payload = dict(item)
            payload["html_gzip"] = bytes(item["html_gzip"]).hex()
            payload["sent_at"] = item["sent_at"].isoformat()
            payload["created_at"] = item["created_at"].isoformat()
            stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _read_cache(path: Path) -> list[dict[str, Any]]:
    docs = []
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            item["html_gzip"] = Binary(bytes.fromhex(item["html_gzip"]))
            item["sent_at"] = datetime.fromisoformat(item["sent_at"])
            item["created_at"] = datetime.fromisoformat(item["created_at"])
            decoded = gzip.decompress(item["html_gzip"])
            if len(decoded) != item["original_html_bytes"] or hashlib.sha256(decoded).hexdigest() != item["original_html_sha256"]:
                raise ValueError(f"staged artifact hash mismatch for {item['_id']}")
            docs.append(item)
    if len(docs) != EXPECTED_ARTIFACTS:
        raise ValueError(f"staging cache count mismatch: {len(docs)}")
    return docs


def _execute(artifacts: Any, documents: list[dict[str, Any]]) -> tuple[int, int, int]:
    existing = {}
    for item in documents:
        old = artifacts.find_one({"_id": item["_id"]}, {"_id": 1, "original_html_sha256": 1})
        if old:
            existing[str(old["_id"])] = old
    conflicts = [key for key, old in existing.items()
                 if old.get("original_html_sha256") != next(d["original_html_sha256"] for d in documents if d["_id"] == key)]
    if conflicts:
        raise ValueError(f"artifact hash conflicts found: {len(conflicts)}")
    inserted = reused = 0
    for item in documents:
        if item["_id"] in existing:
            reused += 1
            continue
        result = artifacts.update_one({"_id": item["_id"]}, {"$setOnInsert": item}, upsert=True)
        if result.upserted_id is not None:
            inserted += 1
        else:
            current = artifacts.find_one({"_id": item["_id"]}, {"original_html_sha256": 1}) or {}
            if current.get("original_html_sha256") != item["original_html_sha256"]:
                raise ValueError(f"concurrent artifact conflict for {item['_id']}")
            reused += 1
    count = sum(artifacts.count_documents({"campaign_id": campaign_id}) for campaign_id in CAMPAIGN_IDS)
    if count != EXPECTED_ARTIFACTS:
        raise ValueError(f"post-write artifact count mismatch: {count}")
    return inserted, reused, count


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--campaign-ids", nargs="+", default=list(CAMPAIGN_IDS))
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    args = parser.parse_args()
    if not Config.MONGO_URI:
        raise RuntimeError("MONGO_URI is not configured")
    client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=15000)
    db = client[Config.DB_NAME]
    try:
        db.command("ping")
        if args.dry_run:
            user = str(Config.GMAIL_USER or "").strip()
            password = str(Config.GMAIL_PASSWORD or "")
            if not user or not password:
                raise RuntimeError("Gmail read-only source credentials unavailable")
            if tuple(args.campaign_ids) != CAMPAIGN_IDS:
                raise ValueError("this backfill is scoped to the two verified owner campaigns")
            rows = _source_rows(db[Config.COLLECTION_CAMPANAS_LOG], tuple(args.campaign_ids))
            documents = _extract_all(rows, user, password)
            _write_cache(documents, args.cache)
            total_raw = sum(int(x["original_html_bytes"]) for x in documents)
            total_gzip = sum(int(x["compressed_bytes"]) for x in documents)
            print(f"MODE=DRY_RUN ARTIFACTS={len(documents)} RAW_BYTES={total_raw} GZIP_BYTES={total_gzip} HASH_VERIFIED={len(documents)}/{len(documents)} MONGO_WRITES=0 CACHE={args.cache}")
            return 0
        documents = _read_cache(args.cache)
        if {d["campaign_id"] for d in documents} != set(args.campaign_ids):
            raise ValueError("staging campaign mismatch")
        inserted, reused, count = _execute(db[EMAIL_ARTIFACT_COLLECTION], documents)
        print(f"MODE=EXECUTE ARTIFACT_COUNT={count} INSERTED={inserted} REUSED={reused} CONFLICTS=0")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"BACKFILL_ABORTED={type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
