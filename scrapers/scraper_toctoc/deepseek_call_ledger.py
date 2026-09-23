"""Durable, secret-free audit ledger for each DeepSeek HTTP attempt."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any


DEEPSEEK_CALL_LEDGER_COLLECTION = "deepseek_call_ledger"


class DeepSeekLedgerError(RuntimeError):
    """Raised when a DeepSeek attempt cannot be durably recorded."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_id(listing_id: str, fingerprint: str, attempt_number: int) -> str:
    raw = json.dumps(
        [str(listing_id), str(fingerprint), int(attempt_number)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DeepSeekCallLedger:
    """Mongo-backed append-only attempt records, with one row per HTTP call.

    A REQUEST_STARTED row is inserted before the provider is called. The same
    row is then completed with the raw response and parser outcome. A compound
    unique index prevents concurrent workers from reusing an attempt number.
    """

    _index_lock = threading.Lock()
    _indexed_collections: set[tuple[str, str]] = set()

    def __init__(self, db: Any, collection_name: str = DEEPSEEK_CALL_LEDGER_COLLECTION):
        if db is None:
            raise DeepSeekLedgerError("Mongo database is required for durable AI logging")
        self.db = db
        self.collection_name = collection_name
        self.collection = db[collection_name]

    def ensure_indexes(self) -> None:
        database_name = str(getattr(self.db, "name", ""))
        key = (database_name, self.collection_name)
        if key in self._indexed_collections:
            return
        with self._index_lock:
            if key in self._indexed_collections:
                return
            try:
                self.collection.create_index(
                    [("listing_id", 1), ("classification_fingerprint", 1), ("attempt_number", 1)],
                    unique=True,
                    name="listing_fingerprint_attempt_unique",
                )
                self.collection.create_index(
                    [("run_id", 1), ("listing_id", 1)],
                    name="run_listing_lookup",
                )
            except Exception as exc:
                raise DeepSeekLedgerError("unable to provision DeepSeek ledger indexes") from exc
            self._indexed_collections.add(key)

    def begin_attempt(self, context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        self.ensure_indexes()
        listing_id = str(context.get("listing_id") or "").strip()
        fingerprint = str(context.get("classification_fingerprint") or "").strip()
        if not listing_id or not fingerprint:
            raise DeepSeekLedgerError("listing_id and classification_fingerprint are required")

        # Retry allocation only if another worker wins the same attempt number.
        for _ in range(5):
            latest = self.collection.find_one(
                {"listing_id": listing_id, "classification_fingerprint": fingerprint},
                sort=[("attempt_number", -1)],
            )
            max_attempts = max(1, int(context.get("max_attempts_per_fingerprint") or 2))
            latest_number = int((latest or {}).get("attempt_number") or 0)
            if latest_number >= max_attempts:
                raise DeepSeekLedgerError("maximum attempts reached for classification fingerprint")
            if latest and str(latest.get("parser_status") or latest.get("status") or "") in {
                "REQUEST_STARTED", "NOT_PARSED", "VALID",
            }:
                raise DeepSeekLedgerError("matching DeepSeek attempt is in-flight or already valid")
            attempt_number = int((latest or {}).get("attempt_number") or 0) + 1
            row = {
                "_id": _stable_id(listing_id, fingerprint, attempt_number),
                "run_id": str(context.get("run_id") or ""),
                "pipeline_item_id": str(context.get("pipeline_item_id") or ""),
                "property_id": str(context.get("property_id") or listing_id),
                "listing_id": listing_id,
                "url": str(context.get("url") or ""),
                "classification_fingerprint": fingerprint,
                "extractor_version": str(context.get("extractor_version") or ""),
                "rules_version": str(context.get("rules_version") or ""),
                "classifier_version": str(context.get("classifier_version") or ""),
                "prompt_version": str(context.get("prompt_version") or ""),
                "model_version": str(context.get("model_version") or request.get("model") or ""),
                "attempt_number": attempt_number,
                "started_at": _now(),
                "status": "REQUEST_STARTED",
                "model_requested": str(request.get("model") or ""),
                "model_returned": "",
                "http_status": None,
                "finish_reason": "",
                "max_tokens_requested": request.get("max_tokens"),
                "max_tokens_effective": request.get("max_tokens"),
                "response_format_effective": request.get("response_format"),
                "temperature_effective": request.get("temperature"),
                "thinking_effective": request.get("thinking"),
                # Reproducible payload; auth headers are deliberately absent.
                "request_payload": dict(request),
                "max_attempts_per_fingerprint": max_attempts,
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "raw_content": "",
                "raw_reasoning_content": "",
                "raw_response_body": "",
                "content_length": 0,
                "parser_status": "REQUEST_STARTED",
                "parser_error": "",
                "classification": "",
                "confidence": None,
                "evidence": [],
                "reason": "",
            }
            try:
                self.collection.insert_one(row)
                return {"_id": row["_id"], "attempt_number": attempt_number}
            except Exception as exc:
                # A duplicate id is a genuine concurrent allocation race; other
                # Mongo failures must stop the request before it consumes API.
                if exc.__class__.__name__ in {"DuplicateKeyError", "BulkWriteError"}:
                    continue
                raise DeepSeekLedgerError("unable to persist DeepSeek attempt before request") from exc
        raise DeepSeekLedgerError("could not allocate a unique DeepSeek attempt number")

    def finish_attempt(self, attempt: dict[str, Any], fields: dict[str, Any]) -> None:
        try:
            completed_at = _now()
            result = self.collection.update_one(
                {"_id": attempt["_id"]},
                {"$set": {**fields, "finished_at": completed_at, "completed_at": completed_at}},
            )
        except Exception as exc:
            raise DeepSeekLedgerError("unable to persist DeepSeek response") from exc
        if not getattr(result, "matched_count", 0):
            raise DeepSeekLedgerError("DeepSeek attempt row disappeared before completion")

    def latest_attempt(self, listing_id: str, fingerprint: str) -> dict[str, Any] | None:
        try:
            valid = self.collection.find_one(
                {
                    "listing_id": str(listing_id),
                    "classification_fingerprint": str(fingerprint),
                    "parser_status": "VALID",
                },
                sort=[("attempt_number", -1)],
            )
            if valid:
                return dict(valid)
            row = self.collection.find_one(
                {"listing_id": str(listing_id), "classification_fingerprint": str(fingerprint)},
                sort=[("attempt_number", -1)],
            )
        except Exception as exc:
            raise DeepSeekLedgerError("unable to read prior DeepSeek attempts") from exc
        return dict(row) if row else None


__all__ = [
    "DEEPSEEK_CALL_LEDGER_COLLECTION",
    "DeepSeekCallLedger",
    "DeepSeekLedgerError",
]
