"""Safely remove test interactions from the two production owner campaigns.

Default execution is read-only. Production writes require --execute and are
performed in one MongoDB transaction after the exact preflight is revalidated.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

from pymongo import MongoClient

from config import Config


COLLECTION_NAME = "ajuste_precio"
DATABASE_NAME = "URLS"
CAMPAIGNS = (
    "owner_price_sucre_wave1_20260928",
    "owner_price_sucre_wave2_20260930",
)
PORTAL_AUTHORIZATION_CODES = frozenset({"5735", "6218", "6478", "6573"})
EMAIL_AUTHORIZATION_CODES = frozenset({"16999", "6308", "6426", "6519", "6673", "6734"})
RESIDUAL_AUTHORIZATION_CODES = frozenset({"5641", "5806"})
AUTHORIZATION_FIELDS = (
    "price_authorized_at",
    "selected_adjustment_type",
    "selected_adjustment_pct",
    "selected_price",
    "selected_price_clp",
    "authorized_price",
    "gradual_authorized_at",
)
DERIVED_EVENT_FIELDS = {
    "last_cta_clicked_at": "cta_clicked",
    "last_price_confirm_page_opened_at": "price_confirm_page_opened",
    "last_confirmation_page_opened_at": "confirmation_page_opened",
    "last_report_opened_at": "report_opened",
    "advisor_review_requested_at": "advisor_review_requested",
}
EXPECTED_EMAIL_TEMPLATE_EVENTS = {
    "advisor_review_requested": 1,
    "confirmation_page_opened": 8,
    "cta_clicked": 11,
    "price_authorized": 6,
    "price_confirm_page_opened": 8,
    "recommended_selected": 6,
    "report_opened": 2,
}
EXPECTED_SOURCELESS_EVENTS = {"cta_clicked": 9, "report_opened": 9}
EXPECTED_PRECHECK = {
    "documents_affected": 29,
    "events_to_remove": 144,
    "email_template_events_before": 42,
    "historical_email_events_before": 18,
}


class CleanupPreflightError(RuntimeError):
    pass


def _surface(event: Mapping[str, Any]) -> Any:
    return event.get("interaction_surface")


def is_owner_portal_link_event(event: Mapping[str, Any]) -> bool:
    """Return true only for explicit portal events or its known legacy form."""
    surface = _surface(event)
    if surface == "EMAIL_TEMPLATE":
        return False
    if surface == "OWNER_PORTAL":
        return True
    return "interaction_surface" not in event and event.get("source") in {"EMAIL", "WHATSAPP"}


def _source_less_historical_email_event(event: Mapping[str, Any]) -> bool:
    return (
        "interaction_surface" not in event
        and "source" not in event
        and event.get("event") in EXPECTED_SOURCELESS_EVENTS
    )


def _email_template_event(event: Mapping[str, Any]) -> bool:
    return _surface(event) == "EMAIL_TEMPLATE"


def _event_time_key(event: Mapping[str, Any]) -> float:
    value = event.get("event_at")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return float("-inf")
    if not isinstance(value, datetime):
        return float("-inf")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).timestamp()


def _latest_event_value(events: list[Mapping[str, Any]], event_name: str) -> Any:
    candidates = [event for event in events if event.get("event") == event_name]
    if not candidates:
        return None
    latest = max(candidates, key=_event_time_key)
    return latest.get("event_at")


def _notification_paths_to_remove(
    notifications: Any, removed_event_ids: set[str]
) -> tuple[list[str], int]:
    paths: list[str] = []
    preserved = 0
    if not isinstance(notifications, Mapping):
        return paths, preserved
    for channel in ("email", "admin_whatsapp"):
        branch = notifications.get(channel)
        if not isinstance(branch, Mapping):
            continue
        for event_name, notification in branch.items():
            if not isinstance(event_name, str) or not isinstance(notification, Mapping):
                preserved += 1
                continue
            event_id = notification.get("event_id")
            if event_id and str(event_id) in removed_event_ids:
                paths.append(f"notifications.{channel}.{event_name}")
            else:
                preserved += 1
    return paths, preserved


def build_document_plan(document: Mapping[str, Any]) -> dict[str, Any] | None:
    all_events = document.get("events") if isinstance(document.get("events"), list) else []
    events_remove = [event for event in all_events if isinstance(event, Mapping) and is_owner_portal_link_event(event)]
    events_keep = [event for event in all_events if not (isinstance(event, Mapping) and is_owner_portal_link_event(event))]
    property_code = str(document.get("property_code") or "")
    removed_auth = [event for event in events_remove if event.get("event") == "price_authorized"]
    kept_auth = [event for event in events_keep if event.get("event") == "price_authorized"]

    reset_authorization = False
    if property_code in PORTAL_AUTHORIZATION_CODES and removed_auth:
        if kept_auth or property_code in EMAIL_AUTHORIZATION_CODES:
            raise CleanupPreflightError(
                f"refusing authorization reset with surviving authorization evidence: {property_code}"
            )
        reset_authorization = True
    if property_code in RESIDUAL_AUTHORIZATION_CODES:
        if kept_auth:
            raise CleanupPreflightError(
                f"residual authorization has a surviving price_authorized event: {property_code}"
            )
        reset_authorization = any(field in document for field in AUTHORIZATION_FIELDS) or document.get("authorization_status") == "PRICE_AUTHORIZED"

    removed_event_ids = {
        str(event.get("event_id")) for event in events_remove if event.get("event_id")
    }
    notification_paths, notifications_preserved = _notification_paths_to_remove(
        document.get("notifications"), removed_event_ids,
    )
    email_notification_paths = [path for path in notification_paths if path.startswith("notifications.email.")]
    if email_notification_paths:
        raise CleanupPreflightError(
            "email notification points to an event proposed for removal; refusing to modify email data"
        )

    if not events_remove and not reset_authorization and not notification_paths:
        return None
    return {
        "_id": document.get("_id"),
        "campaign_id": document.get("campaign_id"),
        "property_code": property_code,
        "property_code_value": document.get("property_code"),
        "events_before": all_events,
        "events_keep": events_keep,
        "events_remove": events_remove,
        "removed_event_ids": removed_event_ids,
        "notification_paths_to_remove": notification_paths,
        "notifications_preserved": notifications_preserved,
        "reset_authorization": reset_authorization,
    }


def _campaign_documents(collection: Any, *, session: Any = None) -> list[dict[str, Any]]:
    query = {"campaign_id": {"$in": list(CAMPAIGNS)}}
    cursor = collection.find(query, session=session) if session is not None else collection.find(query)
    return list(cursor)


def build_preflight(documents: list[Mapping[str, Any]]) -> dict[str, Any]:
    plans = [plan for document in documents if (plan := build_document_plan(document)) is not None]
    event_affected_documents = sum(bool(plan["events_remove"]) for plan in plans)
    all_events = [
        event
        for document in documents
        for event in (document.get("events") if isinstance(document.get("events"), list) else [])
        if isinstance(event, Mapping)
    ]
    removed_events = [event for plan in plans for event in plan["events_remove"]]
    kept_by_document = [event for plan in plans for event in plan["events_keep"]]
    email_events = [event for event in all_events if _email_template_event(event)]
    sourceless_events = [event for event in all_events if _source_less_historical_email_event(event)]
    portal_auth_events = [event for event in removed_events if event.get("event") == "price_authorized"]
    email_auth_events = [event for event in email_events if event.get("event") == "price_authorized"]
    email_auth_codes = {
        str(event.get("property_code") or "")
        for event in email_auth_events
    }
    portal_auth_codes = {
        str(plan["property_code"])
        for plan in plans
        if any(event.get("event") == "price_authorized" for event in plan["events_remove"])
    }
    counters = Counter(event.get("event") for event in email_events)
    sourceless_counters = Counter(event.get("event") for event in sourceless_events)
    notification_remove_count = sum(len(plan["notification_paths_to_remove"]) for plan in plans)
    notification_preserve_count = sum(plan["notifications_preserved"] for plan in plans)
    whatsapp_removed = sum(event.get("event") == "executive_whatsapp_clicked" for event in removed_events)
    email_auth_count = sum(event.get("event") == "price_authorized" for event in email_events)
    sourceless_expected_count = sum(sourceless_counters.values())
    report = {
        "documents_affected": event_affected_documents,
        "documents_modified": len(plans),
        "events_to_remove": len(removed_events),
        "events_to_keep": len(kept_by_document),
        "email_template_events_before": len(email_events),
        "email_template_event_distribution": dict(sorted(counters.items())),
        "historical_email_events_before": len(sourceless_events),
        "historical_email_event_distribution": dict(sorted(sourceless_counters.items())),
        "price_authorizations_portal_to_reset": sorted(portal_auth_codes),
        "price_authorizations_email_to_preserve": sorted(email_auth_codes),
        "notifications_to_remove": notification_remove_count,
        "notifications_to_preserve": notification_preserve_count,
        "owner_whatsapp_test_events": whatsapp_removed,
        "plans": plans,
        "total_campaign_documents": len(documents),
        "sourceful_legacy_events_to_remove": sum(
            "interaction_surface" not in event and event.get("source") in {"EMAIL", "WHATSAPP"}
            for event in removed_events
        ),
        "sourceful_legacy_events_before": sum(
            "interaction_surface" not in event and event.get("source") in {"EMAIL", "WHATSAPP"}
            for event in all_events
        ),
        "source_less_sourceless_expected_count": sourceless_expected_count,
        "email_auth_event_count": email_auth_count,
    }
    return report


def validate_preflight(report: Mapping[str, Any]) -> None:
    for field, expected in EXPECTED_PRECHECK.items():
        if report.get(field) != expected:
            raise CleanupPreflightError(
                f"preflight invariant mismatch for {field}: expected {expected}, got {report.get(field)}"
            )
    if report.get("email_template_event_distribution") != EXPECTED_EMAIL_TEMPLATE_EVENTS:
        raise CleanupPreflightError("EMAIL_TEMPLATE event distribution differs from the approved precheck")
    if report.get("historical_email_event_distribution") != EXPECTED_SOURCELESS_EVENTS:
        raise CleanupPreflightError("source-less historical email event distribution differs from the approved precheck")
    if set(report.get("price_authorizations_portal_to_reset") or []) != PORTAL_AUTHORIZATION_CODES:
        raise CleanupPreflightError("owner-portal authorization codes differ from the approved target list")
    if set(report.get("price_authorizations_email_to_preserve") or []) != EMAIL_AUTHORIZATION_CODES:
        raise CleanupPreflightError("EMAIL_TEMPLATE authorization codes differ from the approved keep list")
    if report.get("sourceful_legacy_events_before") != report.get("sourceful_legacy_events_to_remove"):
        raise CleanupPreflightError("a source-attributed legacy event was not classified for removal")
    if report.get("source_less_sourceless_expected_count") != 18:
        raise CleanupPreflightError("source-less historical events do not match the 9+9 keep invariant")


def _document_update(plan: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    set_values: dict[str, Any] = {"events": plan["events_keep"]}
    unset_values: dict[str, str] = {}
    for field, event_name in DERIVED_EVENT_FIELDS.items():
        value = _latest_event_value(plan["events_keep"], event_name)
        if value is None:
            unset_values[field] = ""
        else:
            set_values[field] = value
    whatsapp_count = sum(
        event.get("event") == "executive_whatsapp_clicked"
        for event in plan["events_keep"]
        if isinstance(event, Mapping)
    )
    if whatsapp_count:
        set_values["owner_whatsapp_click_count"] = whatsapp_count
    else:
        unset_values["owner_whatsapp_click_count"] = ""
    if plan["reset_authorization"]:
        set_values["authorization_status"] = "PENDING"
        unset_values.update({field: "" for field in AUTHORIZATION_FIELDS})
    unset_values.update({path: "" for path in plan["notification_paths_to_remove"]})
    update: dict[str, Any] = {"$set": set_values}
    if unset_values:
        update["$unset"] = unset_values
    query: dict[str, Any] = {
        "_id": plan["_id"],
        "campaign_id": plan["campaign_id"],
        "property_code": plan["property_code_value"],
    }
    if plan["events_before"]:
        query["events"] = plan["events_before"]
    else:
        query["events"] = {"$in": [[], None]}  # no-event residual authorization documents only
    return query, update


def apply_preflight(collection: Any, report: Mapping[str, Any], *, session: Any = None) -> None:
    for plan in report["plans"]:
        query, update = _document_update(plan)
        kwargs = {"session": session} if session is not None else {}
        result = collection.update_one(query, update, **kwargs)
        if result.modified_count != 1:
            raise CleanupPreflightError(
                f"concurrent change or write mismatch for {plan['campaign_id']}:{plan['property_code']}"
            )


def verify_post_cleanup(documents: list[Mapping[str, Any]]) -> dict[str, Any]:
    events = [
        event
        for document in documents
        for event in (document.get("events") if isinstance(document.get("events"), list) else [])
        if isinstance(event, Mapping)
    ]
    email_events = [event for event in events if _email_template_event(event)]
    source_less_events = [event for event in events if _source_less_historical_email_event(event)]
    email_counts = Counter(event.get("event") for event in email_events)
    source_less_counts = Counter(event.get("event") for event in source_less_events)
    owner_events = [event for event in events if event.get("interaction_surface") == "OWNER_PORTAL"]
    legacy_events = [
        event for event in events
        if "interaction_surface" not in event and event.get("source") in {"EMAIL", "WHATSAPP"}
    ]
    portal_auth = [event for event in owner_events if event.get("event") == "price_authorized"]
    email_auth_codes = {
        str(event.get("property_code") or "")
        for event in email_events if event.get("event") == "price_authorized"
    }
    whatsapp_owner_events = [event for event in owner_events if event.get("event") == "executive_whatsapp_clicked"]
    statuses: dict[str, list[Any]] = {}
    for document in documents:
        statuses.setdefault(str(document.get("property_code")), []).append(document.get("authorization_status"))
    report = {
        "OWNER_PORTAL_EVENTS": len(owner_events),
        "LEGACY_OWNER_LINK_EVENTS_WITH_SOURCE": len(legacy_events),
        "EMAIL_TEMPLATE_EVENTS": len(email_events),
        "EMAIL_TEMPLATE_EVENT_DISTRIBUTION": dict(sorted(email_counts.items())),
        "HISTORICAL_SOURCELESS_CTA_CLICKED": source_less_counts.get("cta_clicked", 0),
        "HISTORICAL_SOURCELESS_REPORT_OPENED": source_less_counts.get("report_opened", 0),
        "PORTAL_PRICE_AUTHORIZATIONS": len(portal_auth),
        "PRICE_AUTHORIZED_PROPERTIES": sorted(email_auth_codes),
        "OWNER_WHATSAPP_TEST_EVENTS": len(whatsapp_owner_events),
        "AUTHORIZATION_STATUS_BY_CODE": statuses,
    }
    expected = {
        "OWNER_PORTAL_EVENTS": 0,
        "LEGACY_OWNER_LINK_EVENTS_WITH_SOURCE": 0,
        "EMAIL_TEMPLATE_EVENTS": 42,
        "EMAIL_TEMPLATE_EVENT_DISTRIBUTION": EXPECTED_EMAIL_TEMPLATE_EVENTS,
        "HISTORICAL_SOURCELESS_CTA_CLICKED": 9,
        "HISTORICAL_SOURCELESS_REPORT_OPENED": 9,
        "PORTAL_PRICE_AUTHORIZATIONS": 0,
        "PRICE_AUTHORIZED_PROPERTIES": sorted(EMAIL_AUTHORIZATION_CODES),
        "OWNER_WHATSAPP_TEST_EVENTS": 0,
    }
    for key, expected_value in expected.items():
        if report.get(key) != expected_value:
            raise CleanupPreflightError(
                f"post-cleanup invariant mismatch for {key}: expected {expected_value}, got {report.get(key)}"
            )
    for code in PORTAL_AUTHORIZATION_CODES | RESIDUAL_AUTHORIZATION_CODES:
        if not statuses.get(code) or any(status != "PENDING" for status in statuses[code]):
            raise CleanupPreflightError(f"{code} authorization status is not PENDING")
    return report


def email_integrity_snapshot(documents: list[Mapping[str, Any]]) -> str:
    """Fingerprint protected email events, notifications, and auth bookkeeping."""
    protected = []
    for document in documents:
        events = document.get("events") if isinstance(document.get("events"), list) else []
        preserved_events = [event for event in events if not (isinstance(event, Mapping) and is_owner_portal_link_event(event))]
        notifications = document.get("notifications")
        email_notifications = notifications.get("email") if isinstance(notifications, Mapping) else None
        auth_state = None
        if str(document.get("property_code") or "") in EMAIL_AUTHORIZATION_CODES:
            auth_state = {
                field: document.get(field)
                for field in ("authorization_status", *AUTHORIZATION_FIELDS)
                if field in document
            }
        protected.append({
            "_id": document.get("_id"),
            "preserved_events": preserved_events,
            "email_notifications": email_notifications,
            "email_authorization_state": auth_state,
        })
    return json.dumps(protected, ensure_ascii=False, sort_keys=True, default=str)


def _print_summary(report: Mapping[str, Any], *, stage: str) -> None:
    visible = {key: value for key, value in report.items() if key != "plans"}
    print(stage)
    print(json.dumps(visible, ensure_ascii=False, sort_keys=True, default=str, indent=2))


def _run(*, execute: bool) -> int:
    if Config.DB_NAME != DATABASE_NAME:
        raise CleanupPreflightError(f"refusing unexpected database name: {Config.DB_NAME!r}")
    if not Config.MONGO_URI:
        raise CleanupPreflightError("MONGO_URI is not configured")
    client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=10000)
    try:
        client.admin.command("ping")
        db = client[DATABASE_NAME]
        collection = db[COLLECTION_NAME]
        documents = _campaign_documents(collection)
        protected_email_before = email_integrity_snapshot(documents)
        report = build_preflight(documents)
        validate_preflight(report)
        _print_summary(report, stage="DRY-RUN PLAN" if not execute else "EXECUTE PLAN — REVALIDATED")
        if not execute:
            return 0

        # Re-read and revalidate in the write transaction so new owner activity
        # cannot be silently overwritten between the dry-run and execution.
        with client.start_session() as session:
            with session.start_transaction():
                current_documents = _campaign_documents(collection, session=session)
                current_report = build_preflight(current_documents)
                validate_preflight(current_report)
                if (
                    current_report["events_to_remove"] != report["events_to_remove"]
                    or current_report["documents_affected"] != report["documents_affected"]
                    or current_report["documents_modified"] != report["documents_modified"]
                ):
                    raise CleanupPreflightError("campaign data changed since the immediately preceding dry-run")
                apply_preflight(collection, current_report, session=session)
                post_documents = _campaign_documents(collection, session=session)
                if email_integrity_snapshot(post_documents) != protected_email_before:
                    raise CleanupPreflightError("protected email events, notifications, or authorization fields changed")
                post_report = verify_post_cleanup(post_documents)
            # A completed context manager commits the transaction.
        _print_summary(post_report, stage="POST-CLEANUP READ-ONLY VERIFICATION")
        final_documents = _campaign_documents(collection)
        if email_integrity_snapshot(final_documents) != protected_email_before:
            raise CleanupPreflightError("post-commit email data integrity verification failed")
        verify_post_cleanup(final_documents)
        print("EMAIL_AUTHORIZATIONS_MODIFIED=0")
        print("EMAIL_EVENTS_DELETED=0")
        print("EMAIL_NOTIFICATIONS_DELETED=0")
        return 0
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="write after a validated dry-run and current invariant check")
    args = parser.parse_args(argv)
    try:
        return _run(execute=args.execute)
    except Exception as exc:
        print(f"ABORTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
