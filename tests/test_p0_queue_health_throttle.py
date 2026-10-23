"""P0: health telemetry throttling must not change processing or recovery."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_queue_health_snapshots_are_once_per_minute():
    for filename in ("webhook.py", "chatbot/chatbot_queue.py"):
        source = (ROOT / filename).read_text(encoding="utf-8")
        assert source.count("if time.monotonic() - last_health_refresh >= 60.0:") == 1
        assert "last_health_refresh = time.monotonic()" in source
        assert "if time.monotonic() - last_health_refresh >= 10.0:" not in source


def test_fast_operational_claim_and_retry_loops_are_preserved():
    webhook = (ROOT / "webhook.py").read_text(encoding="utf-8")
    queue = (ROOT / "chatbot/chatbot_queue.py").read_text(encoding="utf-8")
    assert "await asyncio.sleep(5)" in webhook
    assert "LeadProcessingService.claim_next(owner)" in webhook
    assert "await asyncio.sleep(2)" in queue
    assert "reconcile_expired_leases, db" in queue
    assert "processed = await process_one_batch(" in queue
    assert "get_queue_health, db, heartbeat=status" in queue
