"""Standalone worker for the shared demand catalog and grouped scraper jobs.

Run as an independent Render Background Worker (or equivalent), never as part
of the chatbot/webhook process. External scraping remains opt-in by env flag.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import logging
import os
import socket
import time
from typing import Any

from chatbot.storage import get_db
from chatbot.demand_sourcing_engine import (
    WORKER_STATE_COLLECTION,
    build_external_catalog_record,
    build_procasa_catalog_record,
    ensure_indexes,
    index_external_listing,
    index_procasa_listing,
    refresh_catalog_candidate_relations,
    run_one_job,
    sync_catalog_batch,
    utc_now,
)

logger = logging.getLogger("procasa.demand_sourcing_worker")


class _ChangeStreams:
    def __init__(self, db):
        self.db = db
        self.streams: dict[str, Any] = {}
        self.enabled = True

    def connect(self) -> None:
        for collection_name in ("propiedades_captacion", "universo_cartera_prop360"):
            state_id = "catalog_resume:" + collection_name
            state = self.db[WORKER_STATE_COLLECTION].find_one({"_id": state_id}) or {}
            kwargs = {"full_document": "updateLookup", "max_await_time_ms": 1000}
            if state.get("resume_token"):
                kwargs["resume_after"] = state["resume_token"]
            try:
                self.streams[collection_name] = self.db[collection_name].watch(**kwargs)
                logger.info("catalog_change_stream source=%s status=connected", collection_name)
            except Exception as exc:
                self.enabled = False
                logger.warning("catalog_change_stream source=%s status=unavailable error=%s",
                               collection_name, type(exc).__name__)
                for stream in self.streams.values():
                    try:
                        stream.close()
                    except Exception:
                        pass
                self.streams.clear()
                return

    def poll(self, *, now: datetime) -> dict:
        processed = 0
        if not self.enabled:
            return {"enabled": False, "processed": 0}
        for collection_name, stream in list(self.streams.items()):
            try:
                event = stream.try_next()
            except Exception as exc:
                logger.warning("catalog_change_stream source=%s status=read_error error=%s",
                               collection_name, type(exc).__name__)
                try:
                    stream.close()
                except Exception:
                    pass
                self.streams.pop(collection_name, None)
                self.enabled = False
                continue
            if not event:
                continue
            token = event.get("_id")
            operation = event.get("operationType")
            full = event.get("fullDocument") or {}
            if operation == "delete":
                source_id = event.get("documentKey", {}).get("_id")
                source_type = "EXTERNAL" if collection_name == "propiedades_captacion" else "PROCASA"
                catalog = self.db["demand_property_index"].find_one({
                    "source_type": source_type, "source_document_id": source_id,
                })
                if catalog:
                    self.db["demand_property_index"].update_one({"_id": catalog["_id"]}, {
                        "$set": {"published": False, "operational_state": "REMOVED",
                                 "verification_state": "NOT_AVAILABLE", "updated_at": now},
                    })
                    removed = self.db["demand_property_index"].find_one({"_id": catalog["_id"]})
                    refresh_catalog_candidate_relations(self.db, removed, now=now)
            elif full:
                if collection_name == "propiedades_captacion":
                    record = build_external_catalog_record(full, now=now)
                else:
                    record = build_procasa_catalog_record(full, now=now)
                if record:
                    if collection_name == "propiedades_captacion":
                        index_external_listing(self.db, full, inverse_match=True,
                                               observed_at=now, now=now)
                    else:
                        index_procasa_listing(self.db, full, inverse_match=True, now=now)
            if token:
                self.db[WORKER_STATE_COLLECTION].update_one({"_id": "catalog_resume:" + collection_name}, {
                    "$set": {"resume_token": token, "updated_at": now},
                    "$setOnInsert": {"source_collection": collection_name, "created_at": now},
                }, upsert=True)
            processed += 1
        return {"enabled": self.enabled, "processed": processed}


def _sync_recent_updates(db, *, now: datetime, batch_size: int = 100) -> dict:
    """Bounded fallback for clusters where change streams are unavailable.

    This is a background-worker read with a narrow updated-at window. Source
    collections are not scanned by an HTTP request or lead webhook.
    """
    cutoff = now - timedelta(hours=2)
    totals = {}
    for collection_name in ("propiedades_captacion", "universo_cartera_prop360"):
        query = {"$or": [{"updated_at": {"$gte": cutoff}},
                         {"processed_at": {"$gte": cutoff}},
                         {"last_seen_at": {"$gte": cutoff}}]}
        try:
            rows = list(db[collection_name].find(query).sort("updated_at", -1).limit(batch_size))
        except Exception as exc:
            logger.warning("catalog_recent_sync source=%s error=%s", collection_name, type(exc).__name__)
            rows = []
        indexed = 0
        for row in rows:
            try:
                result = (index_external_listing(db, row, inverse_match=True,
                                                 observed_at=now, now=now)
                          if collection_name == "propiedades_captacion"
                          else index_procasa_listing(db, row, inverse_match=True, now=now))
                indexed += bool(result)
            except Exception as exc:
                logger.warning("catalog_recent_sync source=%s record_error=%s", collection_name, type(exc).__name__)
        totals[collection_name] = {"seen": len(rows), "indexed": indexed}
    return totals


def run_worker(*, once: bool = False, poll_seconds: int = 5,
               batch_size: int = 200) -> int:
    db = get_db()
    ensure_indexes(db)
    worker_id = f"demand-source-{socket.gethostname()}-{os.getpid()}"
    external_enabled = os.getenv("PROCASA_DEMAND_SOURCING_EXTERNAL_SCRAPE_ENABLED", "false").strip().lower() == "true"
    demand_enabled = os.getenv("PROCASA_DEMAND_SOURCING_ENABLED", "0").strip() == "1"
    quota = int(os.getenv("PROCASA_DEMAND_SOURCING_DAILY_QUOTA", "20") or "20")
    scraper_runner = None
    if external_enabled:
        from chatbot.demand_sourcing_scrapers import run_existing_scraper
        scraper_runner = run_existing_scraper
    streams = _ChangeStreams(db)
    if not once:
        streams.connect()
    logger.info("worker_start id=%s external_scraping=%s poll_seconds=%s", worker_id,
                str(external_enabled).lower(), poll_seconds)
    last_fallback_sync = 0.0
    while True:
        now = utc_now()
        try:
            catalog = sync_catalog_batch(db, batch_size=batch_size, now=now)
            changes = streams.poll(now=now) if streams.enabled else {"enabled": False, "processed": 0}
            fallback = {}
            if not streams.enabled and time.monotonic() - last_fallback_sync >= 300:
                fallback = _sync_recent_updates(db, now=now, batch_size=min(batch_size, 200))
                last_fallback_sync = time.monotonic()
            demand_backfill = {}
            if demand_enabled:
                try:
                    from chatbot.active_search_followup import refresh_active_search_batch
                    demand_backfill = refresh_active_search_batch(db, limit=20, now=now)
                except Exception as exc:
                    logger.warning("demand_case_backfill error=%s", type(exc).__name__)
            db[WORKER_STATE_COLLECTION].update_one({"_id": "worker_heartbeat"}, {
                "$set": {"worker_id": worker_id, "external_scraping_enabled": external_enabled,
                         "change_streams_enabled": streams.enabled, "catalog_sync": catalog,
                         "change_stream_events": changes.get("processed", 0),
                         "fallback_sync": fallback, "demand_case_backfill": demand_backfill,
                         "demand_sourcing_enabled": demand_enabled, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            }, upsert=True)
            job = run_one_job(db, worker_id=worker_id, now=now,
                              scraper_runner=scraper_runner,
                              external_enabled=external_enabled,
                              quota_per_source=quota)
            if job.get("status") != "idle":
                logger.info("job_complete id=%s status=%s", job.get("job_id"), job.get("status"))
            if once:
                return 0
            time.sleep(max(1, min(poll_seconds, 60)))
        except KeyboardInterrupt:
            logger.info("worker_stop id=%s", worker_id)
            return 0
        except Exception as exc:
            logger.exception("worker_cycle_failed error=%s", type(exc).__name__)
            if once:
                return 1
            time.sleep(max(5, min(poll_seconds, 60)))


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one bootstrap/job cycle and exit")
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()
    return run_worker(once=args.once, poll_seconds=args.poll_seconds,
                      batch_size=args.batch_size)


if __name__ == "__main__":
    raise SystemExit(main())
