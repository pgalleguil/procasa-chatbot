"""Opt-in V2 worker adapter with runtime ownership enforced by the queue.

The adapter is intentionally dependency-injected: the production response
planner/final barrier supplies ``response_factory`` and the provider client is
supplied as ``sender``.  This keeps Release A from changing prompts or V1
behaviour while making the future V2 worker use the same claim and network
fences as the isolated validation path.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from . import runtime_control as runtime
from . import turn_queue as queue


def v2_worker_enabled(db) -> bool:
    control = runtime.get_runtime_control(db)
    return control.get("mode") == runtime.V2_ACTIVE


async def process_one_turn(
    db,
    *,
    worker_id: str,
    response_factory: Callable[[Mapping[str, Any]], str | Awaitable[str]],
    sender: Callable[[str, str], Mapping[str, Any]],
    now: datetime | None = None,
    human_active: Callable[[], bool] | bool = False,
) -> queue.DeliveryResult | None:
    """Claim, render/validate upstream, and deliver one V2 turn.

    ``response_factory`` is the boundary for the existing production core;
    this function never calls a model itself.  If rendering fails, the turn
    is terminalized without invoking the provider.
    """
    turn = await asyncio.to_thread(queue.claim_pending_turn, db, worker_id=worker_id, now=now)
    if not turn:
        return None
    try:
        response = response_factory(turn)
        if hasattr(response, "__await__"):
            response = await response
    except asyncio.CancelledError:
        await asyncio.to_thread(
            queue.finalize_turn, db, turn_id=turn["_id"], worker_id=worker_id,
            state=queue.TURN_FAILED_TERMINAL, now=now,
            error="v2_worker_cancelled_before_provider",
        )
        raise
    except Exception as exc:
        await asyncio.to_thread(
            queue.finalize_turn, db, turn_id=turn["_id"], worker_id=worker_id,
            state=queue.TURN_FAILED_TERMINAL, now=now,
            error=f"v2_response_factory:{type(exc).__name__}",
        )
        return queue.DeliveryResult(state=queue.TURN_FAILED_TERMINAL)
    return await asyncio.to_thread(
        queue.deliver_claimed_turn,
        db, turn=turn, worker_id=worker_id, response=str(response or ""),
        sender=sender, human_active=human_active, now=now,
    )


async def worker_loop(
    db,
    *,
    worker_id: str,
    response_factory: Callable[[Mapping[str, Any]], str | Awaitable[str]],
    sender: Callable[[str, str], Mapping[str, Any]],
    stop_event: asyncio.Event,
    poll_seconds: float = 2.0,
) -> None:
    """Run only while V2 is active; shutdown never changes runtime control."""
    while not stop_event.is_set():
        processed = await process_one_turn(
            db, worker_id=worker_id, response_factory=response_factory,
            sender=sender,
        )
        if processed is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(poll_seconds, 0.05))
            except asyncio.TimeoutError:
                pass

