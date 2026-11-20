"""Regressions for read-only event-loop stall sampling."""
import inspect
import logging

from chatbot.event_loop_stall_probe import EventLoopStallProbe, _safe_stack_location


def test_stall_probe_emits_once_per_cooldown_without_leaking_payload(caplog):
    now = [100.0]
    frame = inspect.currentframe()
    log = logging.getLogger("test.procasa.loop_probe")
    probe = EventLoopStallProbe(
        12345, logger=log,
        sample_interval_seconds=0.1,
        stale_after_seconds=1.35,
        cooldown_seconds=60,
        clock=lambda: now[0],
        get_frame=lambda ident: frame,
    )
    probe.pulse()
    with caplog.at_level(logging.WARNING, logger=log.name):
        now[0] = 101.34
        assert probe.sample_once() is False
        now[0] = 101.40
        assert probe.sample_once() is True
        assert probe.sample_once() is False
        probe.pulse()
        now[0] = 103.0
        assert probe.sample_once() is False  # cooldown
        now[0] = 162.0
        assert probe.sample_once() is True
    assert len(caplog.records) == 2
    for record in caplog.records:
        assert "EVENT_LOOP_STALL_SAMPLE" in record.message
        assert "live_thread_frame_not_causality" in record.message
        assert "test_event_loop_stall_probe.py" in record.message
        assert "secret-text" not in record.message
        assert "locals" not in record.message


def test_safe_stack_location_contains_only_file_function_and_line():
    secret_local_value = "secret-text"
    signature = _safe_stack_location(inspect.currentframe(), depth=2)
    assert "test_safe_stack_location_contains_only_file_function_and_line" in signature
    assert "test_event_loop_stall_probe.py" in signature
    assert secret_local_value not in signature
    assert "secret_local_value" not in signature


def test_probe_start_is_idempotent_and_stop_is_nonblocking():
    probe = EventLoopStallProbe(12345, logger=logging.getLogger(__name__))
    probe.start()
    first = probe._thread
    probe.start()
    assert first is probe._thread
    probe.stop()
    assert probe._stop_event.is_set()
