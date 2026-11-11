"""Read-only account-level WaSender message-log usage checks.

This counts all API message logs across every WaSender session belonging to
the configured account over a conservative rolling 24-hour window. It fails
closed when session logs cannot prove complete coverage.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


class WaSenderUsageUnavailable(RuntimeError):
    pass


def _body(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise WaSenderUsageUnavailable("wasender_usage_invalid_json") from exc
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise WaSenderUsageUnavailable("wasender_usage_api_error")
    if int(getattr(response, "status_code", 0) or 0) != 200:
        raise WaSenderUsageUnavailable("wasender_usage_http_error")
    return payload


def _timestamp(value: Any, *, naive_timezone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str) and value.strip():
        try:
            stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise WaSenderUsageUnavailable("wasender_usage_timestamp_invalid") from exc
    else:
        raise WaSenderUsageUnavailable("wasender_usage_timestamp_missing")
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        stamp = stamp.replace(tzinfo=naive_timezone)
    return stamp.astimezone(timezone.utc)


def account_attempts_last_24h(*, now: datetime, base_url: str, personal_access_token: str,
                              http_get: Callable[..., Any] = requests.get,
                              naive_timezone_name: str = "UTC",
                              page_size: int = 100) -> dict[str, Any]:
    """Scan all enabled session logs without retaining old records in memory."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise WaSenderUsageUnavailable("wasender_usage_now_requires_timezone")
    if not personal_access_token or not base_url.startswith("https://"):
        raise WaSenderUsageUnavailable("wasender_usage_provider_not_configured")
    try:
        naive_timezone = ZoneInfo(naive_timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise WaSenderUsageUnavailable("wasender_log_timezone_invalid") from exc
    root = base_url.rstrip("/")
    account_headers = {
        "Authorization": f"Bearer {personal_access_token}", "Accept": "application/json",
    }
    try:
        sessions_payload = _body(http_get(
            f"{root}/whatsapp-sessions", headers=account_headers, timeout=(3, 12),
        ))
    except WaSenderUsageUnavailable:
        raise
    except Exception as exc:
        raise WaSenderUsageUnavailable("wasender_sessions_request_failed") from exc
    sessions = sessions_payload.get("data")
    if isinstance(sessions, dict):
        sessions = sessions.get("data")
    if not isinstance(sessions, list) or not sessions:
        raise WaSenderUsageUnavailable("wasender_sessions_unavailable")

    cutoff = now.astimezone(timezone.utc) - timedelta(hours=24)
    total = 0
    log_rows = 0
    pages_read = 0
    for session in sessions:
        if not isinstance(session, dict) or not session.get("id"):
            raise WaSenderUsageUnavailable("wasender_session_metadata_incomplete")
        if session.get("log_messages") is not True:
            raise WaSenderUsageUnavailable("wasender_session_message_logs_disabled")
        session_id = quote(str(session["id"]), safe="")
        try:
            detail_payload = _body(http_get(
                f"{root}/whatsapp-sessions/{session_id}",
                headers=account_headers, timeout=(3, 12),
            ))
        except WaSenderUsageUnavailable:
            raise
        except Exception as exc:
            raise WaSenderUsageUnavailable("wasender_session_details_request_failed") from exc
        detail = detail_payload.get("data")
        if not isinstance(detail, dict) or detail.get("log_messages") is not True:
            raise WaSenderUsageUnavailable("wasender_session_details_incomplete")
        page = 1
        last_page = None
        while last_page is None or page <= last_page:
            if pages_read >= 100_000:
                raise WaSenderUsageUnavailable("wasender_usage_pagination_safety_limit")
            try:
                payload = _body(http_get(
                    f"{root}/whatsapp-sessions/{session_id}/message-logs",
                    headers=account_headers, params={"page": page, "per_page": page_size},
                    timeout=(3, 12),
                ))
            except WaSenderUsageUnavailable:
                raise
            except Exception as exc:
                raise WaSenderUsageUnavailable("wasender_message_logs_request_failed") from exc
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise WaSenderUsageUnavailable("wasender_message_logs_shape_invalid")
            try:
                last_page = int(data.get("last_page"))
                current_page = int(data.get("current_page", page))
            except (TypeError, ValueError) as exc:
                raise WaSenderUsageUnavailable("wasender_message_logs_pagination_invalid") from exc
            if last_page < 1 or current_page != page or page > last_page:
                raise WaSenderUsageUnavailable("wasender_message_logs_pagination_invalid")
            for row in data["data"]:
                if not isinstance(row, dict):
                    raise WaSenderUsageUnavailable("wasender_message_log_row_invalid")
                created = _timestamp(row.get("created_at"), naive_timezone=naive_timezone)
                log_rows += 1
                if created >= cutoff:
                    # Count every provider API attempt record, including failed
                    # states, because the provider limits requests, not delivery.
                    total += 1
            pages_read += 1
            page += 1
    return {
        "attempts_last_24h": total,
        "sessions_scanned": len(sessions),
        "log_rows_scanned": log_rows,
        "pages_scanned": pages_read,
        "source": "wasender_session_message_logs",
        "window_start_utc": cutoff.isoformat(),
    }
