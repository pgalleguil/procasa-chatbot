"""No-cache Yapo availability checks routed through the configured FlashProxy only."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scrapers.scraper_yapo.config import get_config
from scrapers.scraper_yapo.downloader import download_html
from scrapers.scraper_yapo.proxy_manager import ProxyManager
from scrapers.scraper_yapo.staging import OPERATIONAL_COLLECTION, SOURCE_PORTAL, STAGING_COLLECTION, STAGING_DATABASE

DEFAULT_IDS = ("32299493", "32632678", "32728499", "32676021", "33211883", "33211046", "33210247")


def _flashproxy_only_manager(config: Any) -> ProxyManager:
    manager = ProxyManager.from_config(config)
    if manager.provider != "flashproxy" or len(manager._proxies) != 1:
        raise RuntimeError("No single verified FlashProxy endpoint is configured; no Yapo request was attempted.")
    parsed = urlsplit(manager._proxies[0])
    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "flashproxy.io" and not host.endswith(".flashproxy.io"):
        raise RuntimeError("Configured proxy is not on the FlashProxy domain; no Yapo request was attempted.")
    if not parsed.username or not parsed.password:
        raise RuntimeError("FlashProxy authentication is missing; no Yapo request was attempted.")
    return manager


def _record_check(collection: Any, listing_id: str, item: dict[str, Any], *, target: str) -> None:
    checked_at = item["availability_checked_at"]
    check_entry = {
        "checked_at": checked_at,
        "status": item["availability_status"],
        "source": "flashproxy",
        "http_status": item.get("http_status"),
        "fetch_source": item.get("fetch_source", "proxy_error"),
        "html_validation_status": item.get("html_validation_status", ""),
        "evidence": list(item.get("availability_evidence") or []),
        "html_sha256": item.get("html_sha256", ""),
        "local_html_path": item.get("local_html_path", ""),
        "run_id": item["run_id"],
    }
    query = (
        {"origen": SOURCE_PORTAL, "listing_id": str(listing_id)}
        if target == OPERATIONAL_COLLECTION
        else {"source_portal": SOURCE_PORTAL, "listing_id": str(listing_id)}
    )
    fields = {
        "availability_status": item["availability_status"],
        "availability_checked_at": checked_at,
        "availability_source": "flashproxy",
        "availability_evidence": list(item.get("availability_evidence") or []),
        "availability_http_status": item.get("http_status"),
        "availability_html_validation_status": item.get("html_validation_status", ""),
        "availability_html_sha256": item.get("html_sha256", ""),
        "availability_live_run_id": item["run_id"],
    }
    if target == STAGING_COLLECTION:
        fields.update({
            "staging_only": True,
            "crm_promotion_allowed": False,
            "distribution_blocked": True,
            "assignment_ready": False,
        })
    result = collection.update_one(query, {"$set": fields, "$push": {"availability_checks": check_entry}})
    if int(getattr(result, "matched_count", 0) or 0) != 1:
        raise RuntimeError(f"Staging listing {listing_id} was missing; check result was not saved.")


def run_checks(ids: list[str], *, target: str = OPERATIONAL_COLLECTION) -> dict[str, Any]:
    if target not in {STAGING_COLLECTION, OPERATIONAL_COLLECTION}:
        raise ValueError("Availability checks can write only to the Yapo staging or operational collection.")
    config = get_config()
    if config.mongo_db != STAGING_DATABASE or config.mongo_collection != OPERATIONAL_COLLECTION:
        raise RuntimeError("Atlas config does not match URLS.propiedades_captacion.")
    host = urlsplit(config.mongo_uri).hostname or ""
    if not host.endswith("mongodb.net"):
        raise RuntimeError("Mongo host is not Atlas; no database write was attempted.")
    proxy_manager = _flashproxy_only_manager(config)

    from pymongo import MongoClient

    client = MongoClient(config.mongo_uri, serverSelectionTimeoutMS=20000, connectTimeoutMS=10000)
    try:
        client.admin.command("ping")
        database = client[STAGING_DATABASE]
        names = database.list_collection_names()
        if target not in names:
            raise RuntimeError(f"Yapo target collection {target} is missing; no live check started.")
        collection = database[target]
        run_id = config.generate_batch_id().replace("owner_hunt", "yapo_live")
        results: list[dict[str, Any]] = []
        stopped_after_proxy_failure = False

        for listing_id in ids:
            listing_id = str(listing_id).strip()
            identity_query = (
                {"origen": SOURCE_PORTAL, "listing_id": listing_id}
                if target == OPERATIONAL_COLLECTION
                else {"source_portal": SOURCE_PORTAL, "listing_id": listing_id}
            )
            staged = collection.find_one(
                identity_query,
                {"url": 1, "canonical_url": 1, "_id": 0},
            )
            if not staged:
                results.append({"listing_id": listing_id, "status": "NOT_CHECKED", "reason": "not_in_staging"})
                continue
            url = str(staged.get("url") or staged.get("canonical_url") or "")
            if not url or (urlsplit(url).hostname or "").lower() not in {"www.yapo.cl", "yapo.cl"}:
                results.append({"listing_id": listing_id, "status": "NOT_CHECKED", "reason": "invalid_yapo_url"})
                continue
            if stopped_after_proxy_failure:
                results.append({"listing_id": listing_id, "status": "NOT_CHECKED", "reason": "previous_proxy_request_failed"})
                continue

            checked_at = datetime.now(timezone.utc).isoformat()
            try:
                downloaded = download_html(
                    url,
                    config,
                    proxy_manager,
                    batch_id=run_id,
                    force_refresh=True,
                )
                if downloaded.proxy_provider != "flashproxy" or downloaded.fetch_source != "requests_proxy":
                    raise RuntimeError("unexpected_transport_provider")
                html_sha = hashlib.sha256(downloaded.html.encode("utf-8")).hexdigest()
                item = {
                    "listing_id": listing_id,
                    "availability_status": downloaded.availability_status,
                    "availability_checked_at": downloaded.availability_checked_at or checked_at,
                    "availability_evidence": downloaded.availability_evidence,
                    "http_status": downloaded.status_code,
                    "fetch_source": downloaded.fetch_source,
                    "html_validation_status": downloaded.validation_status,
                    "html_sha256": html_sha,
                    "local_html_path": str(downloaded.html_path),
                    "run_id": run_id,
                }
                _record_check(collection, listing_id, item, target=target)
                results.append({
                    "listing_id": listing_id,
                    "status": item["availability_status"],
                    "http_status": item["http_status"],
                    "html_validation_status": item["html_validation_status"],
                    "availability_source": "flashproxy",
                    "availability_evidence": item["availability_evidence"],
                })
            except Exception as exc:
                item = {
                    "listing_id": listing_id,
                    "availability_status": "NO_VERIFICADO",
                    "availability_checked_at": checked_at,
                    "availability_evidence": [f"flashproxy_request_failed:{type(exc).__name__}"],
                    "http_status": None,
                    "fetch_source": "proxy_error",
                    "html_validation_status": "",
                    "html_sha256": "",
                    "local_html_path": "",
                    "run_id": run_id,
                }
                _record_check(collection, listing_id, item, target=target)
                results.append({
                    "listing_id": listing_id,
                    "status": "NO_VERIFICADO",
                    "availability_source": "flashproxy",
                    "reason": type(exc).__name__,
                })
                stopped_after_proxy_failure = True

        return {
            "run_id": run_id,
            "requested_ids": ids,
            "checks": results,
            "attempted": sum(item.get("status") != "NOT_CHECKED" for item in results),
            "side_effects": {"staging_updates": sum(item.get("status") != "NOT_CHECKED" for item in results) if target == STAGING_COLLECTION else 0,
                             "operational_collection_writes": sum(item.get("status") != "NOT_CHECKED" for item in results) if target == OPERATIONAL_COLLECTION else 0,
                             "deepseek_calls": 0,
                             "assignments": 0, "distribution": 0, "notifications": 0},
            "proxy_provider": "flashproxy",
            "cache_bypassed": True,
            "target_collection": f"{STAGING_DATABASE}.{target}",
        }
    finally:
        client.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", action="append", default=[], help="Yapo listing ID; may be repeated")
    parser.add_argument("--target", choices=("staging", "operational"), default="operational")
    return parser


if __name__ == "__main__":
    args = _parser().parse_args()
    target = OPERATIONAL_COLLECTION if args.target == "operational" else STAGING_COLLECTION
    report = run_checks(args.ids or list(DEFAULT_IDS), target=target)
    print(json.dumps(report, ensure_ascii=False, indent=2))
