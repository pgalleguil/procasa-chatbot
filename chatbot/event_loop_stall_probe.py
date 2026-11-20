"""Low-overhead diagnostics for stalls of the live asyncio event-loop thread.

The event-loop monitor sees the *delay after it finishes*, when the offending
frame has already returned. This daemon samples the loop thread while the
heartbeat is stale and logs filenames/function names/line numbers ONLY.
No locals, request payloads, phone numbers, message text, or Mongo documents.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Callable


def _safe_stack_location(frame: object, *, depth: int = 6) -> str:
    """Return code locations only; never format source text or frame locals."""
    locations: list[str] = []
    for _ in range(depth):
        if frame is None:
            break
        code = frame.f_code
        locations.append(
            f"{os.path.basename(code.co_filename)}:{code.co_name}:{frame.f_lineno}"
        )
        frame = frame.f_back
    return " <- ".join(locations) if locations else "frame_unavailable"


class EventLoopStallProbe:
    """Sample a stalled event-loop thread, at most once per cooldown period.

    The monitor normally ticks every second. A sample is attempted only once
    the time since the latest tick exceeds one second plus the stall threshold.
    This is for diagnosis, not an attribution of blame or an automatic recovery.
    """

    def __init__(
        self,
        loop_thread_ident: int,
        *,
        logger: logging.Logger,
        sample_interval_seconds: float = 0.2,
        stale_after_seconds: float = 1.35,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        get_frame: Callable[[int], object] | None = None,
    ) -> None:
        self._loop_thread_ident = loop_thread_ident
        self._logger = logger
        self._sample_interval = max(float(sample_interval_seconds), 0.1)
        self._stale_after = max(float(stale_after_seconds), 1.1)
        self._cooldown = max(float(cooldown_seconds), 1.0)
        self._clock = clock
        self._get_frame = get_frame or (lambda ident: sys._current_frames().get(ident))
        self._last_pulse = clock()
        self._last_report = float("-inf")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def pulse(self) -> None:
        self._last_pulse = self._clock()

    def sample_once(self) -> bool:
        """Return True only when a rate-limited forensic sample was logged."""
        now = self._clock()
        stale_for = now - self._last_pulse
        if stale_for < self._stale_after or now - self._last_report < self._cooldown:
            return False
        self._last_report = now
        try:
            frame = self._get_frame(self._loop_thread_ident)
            location = _safe_stack_location(frame)
            # The asyncio selector may be idle while a scheduling delay occurs:
            # record its actual frame rather than alleging a blocking function.
            self._logger.warning(
                "[EVENT_LOOP_STALL_SAMPLE] stale_ms=%d "
                "event_loop_thread_site=%s evidence=live_thread_frame_not_causality",
                round(stale_for * 1000),
                location,
            )
        except Exception as exc:
            self._logger.debug(
                "[EVENT_LOOP_STALL_SAMPLE] collection_failed=%s",
                type(exc).__name__,
            )
        return True

    def _run(self) -> None:
        while not self._stop_event.wait(self._sample_interval):
            self.sample_once()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="procasa_loop_stall_probe"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        # Do not block shutdown or asyncio cancellation to join a daemon.


