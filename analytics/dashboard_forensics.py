"""Temporary, aggregate-only diagnostics for the Leads Dashboard audit.

This module deliberately has no Mongo or application behaviour of its own.  It
only supplies redacted identifiers, process facts and structured diagnostic
logging used by the dashboard request/prewarm paths.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

logger = logging.getLogger(__name__)
PROCESS_STARTED_MONOTONIC = time.monotonic()
PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()
_STATE_LOCK = Lock()
_ACTIVE_BACKGROUND: set[str] = set()
_FIRST_REQUEST_LOGGED = False


def key_hash(key: str) -> str:
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:12]


def request_id() -> str:
    return uuid4().hex[:16]


def _rss_mb() -> float | None:
    try:
        if os.name == "posix":
            value = int(open("/proc/self/statm", encoding="ascii").read().split()[1])
            return round(value * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024, 1)
        # Windows fallback without adding psutil.
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("page_fault_count", wintypes.DWORD),
                        ("peak_ws", ctypes.c_size_t), ("ws", ctypes.c_size_t),
                        ("peak_pf", ctypes.c_size_t), ("pool_paged", ctypes.c_size_t),
                        ("pool_nonpaged", ctypes.c_size_t), ("pagefile", ctypes.c_size_t),
                        ("peak_pagefile", ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        return round(counters.ws / 1024 / 1024, 1) if ok else None
    except Exception:
        return None


def process_facts() -> dict:
    return {
        "pid": os.getpid(),
        "process_started_at": PROCESS_STARTED_AT,
        "process_uptime_seconds": round(time.monotonic() - PROCESS_STARTED_MONOTONIC, 3),
        "rss_mb": _rss_mb(),
        "render_instance_id": os.getenv("RENDER_INSTANCE_ID") or None,
        "thread": threading.current_thread().name,
    }


def emit(prefix: str, payload: dict) -> None:
    # JSON is intentionally built from aggregate fields supplied by callers;
    # no document, identity or raw cache key is accepted here.
    logger.info("%s %s", prefix, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def begin_background(key: str, event: str, endpoint: str, age_before: float | None = None) -> str:
    token = f"{key_hash(key)}:{uuid4().hex}"
    with _STATE_LOCK:
        _ACTIVE_BACKGROUND.add(key)
    emit("[DASHBOARD_PREWARM]", {
        **process_facts(), "event": event, "endpoint": endpoint,
        "timestamp": datetime.now(timezone.utc).isoformat(), "cache_key_hash": key_hash(key),
        "age_before": None if age_before is None else round(age_before, 1), "duration_ms": 0.0,
    })
    return token


def end_background(key: str, endpoint: str, event: str, started: float, age_before: float | None = None) -> None:
    with _STATE_LOCK:
        _ACTIVE_BACKGROUND.discard(key)
    emit("[DASHBOARD_PREWARM]", {
        **process_facts(), "event": event, "endpoint": endpoint,
        "timestamp": datetime.now(timezone.utc).isoformat(), "cache_key_hash": key_hash(key),
        "age_before": None if age_before is None else round(age_before, 1),
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    })


def background_active(key: str) -> bool:
    with _STATE_LOCK:
        return key in _ACTIVE_BACKGROUND


def emit_cache_evict(entries_before: int, evicted_key: str, evicted_at: float,
                     inserted_key: str, pinned_keys: set[str]) -> None:
    emit("[DASHBOARD_CACHE_EVICT]", {
        **process_facts(), "entries_before": entries_before,
        "evicted_key_hash": key_hash(evicted_key),
        "evicted_age_seconds": round(max(0.0, time.time() - evicted_at), 1),
        "evicted_is_pinned": evicted_key in pinned_keys,
        "inserted_key_hash": key_hash(inserted_key),
        "inserted_is_pinned": inserted_key in pinned_keys,
    })


def emit_request(payload: dict) -> None:
    global _FIRST_REQUEST_LOGGED
    emit("[DASHBOARD_PERF]", payload)
    with _STATE_LOCK:
        first = not _FIRST_REQUEST_LOGGED
        _FIRST_REQUEST_LOGGED = True
    if first:
        emit("[DASHBOARD_PREWARM]", {
            **process_facts(), "event": "first_dashboard_request",
            "endpoint": payload.get("endpoint"), "timestamp": datetime.now(timezone.utc).isoformat(),
            "cache_key_hash": payload.get("cache_key_hash"), "age_before": payload.get("cache_age_seconds"),
            "duration_ms": 0.0, "request_id": payload.get("request_id"),
        })
