"""Opt-in V2 worker adapter with runtime ownership enforced by the queue.

The adapter is intentionally dependency-injected: the production response
planner/final barrier supplies ``response_factory`` and the provider client is
supplied as ``sender``.  This keeps Release A from changing prompts or V1
behaviour while making the future V2 worker use the same claim and network
fences as the isolated validation path.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from . import runtime_control as runtime
from . import turn_queue as queue


logger = logging.getLogger(__name__)
PRODUCTION_STATUS_KEY = "chatbot_response_v2"


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


def _v2_generation_id(turn: Mapping[str, Any]) -> str:
    return f"v2:{turn.get('_id')}"


def _human_ownership_active(db, phone: str) -> bool:
    """Read the existing human-takeover flags immediately before delivery."""
    lead = db["leads"].find_one(
        {"phone": phone},
        {"human_active": 1, "conversation_owner": 1},
    ) or {}
    return bool(lead.get("human_active")) or str(
        lead.get("conversation_owner") or ""
    ).strip().lower() == "human"


async def production_worker_loop(*, status: dict[str, Any],
                                stop_event: asyncio.Event | None = None,
                                worker_id: str | None = None,
                                poll_seconds: float = 2.0) -> None:
    """Run the V2 worker in standby or active mode under runtime control.

    Release A starts this loop with V1 active.  It still registers a real
    liveness heartbeat, but ``claim_pending_turn`` consults the canonical
    runtime control before claiming anything.  The production callback uses
    the existing core and final response barrier; it does not introduce a
    second conversational implementation.
    """
    stop_event = stop_event or asyncio.Event()
    worker_id = worker_id or f"chatbot_response_v2_{id(status)}"
    metrics: dict[str, Any] = {
        "turns_claimed": 0,
        "turns_completed": 0,
        "provider_sends": 0,
        "provider_unknown": 0,
        "response_factory_errors": 0,
        "final_barrier_suppressed": 0,
    }
    db = None
    current_phone = {"value": ""}

    def _update_status(control: Mapping[str, Any] | None = None) -> None:
        selected = dict(control or {})
        mode = selected.get("mode") or runtime.V1_ACTIVE
        generation = int(selected.get("generation", 1) or 1)
        status.update({
            "status": "running",
            "health": "ACTIVE" if mode == runtime.V2_ACTIVE else "STANDBY",
            "registered": True,
            "worker_id": worker_id,
            "last_heartbeat": queue.utc_now().isoformat(),
            "runtime_mode": mode,
            "runtime_generation": generation,
            "claim_enabled": mode in {runtime.V2_ACTIVE, runtime.DRAINING_TO_V1},
            "send_enabled": mode == runtime.V2_ACTIVE,
            "metrics": dict(metrics),
        })
        if db is not None:
            try:
                status["queue"] = queue.queue_snapshot(db)
            except Exception:
                # Liveness must remain visible even if the diagnostic query
                # is temporarily unavailable.
                status["queue"] = {"metrics_available": False}

    async def _response_factory(turn: Mapping[str, Any]) -> str:
        from chatbot.core import process_user_message_sync
        from chatbot.final_response_barrier import admit_customer_response

        current_phone["value"] = str(turn.get("phone") or "")
        generation_id = _v2_generation_id(turn)
        snapshot = list(turn.get("snapshot") or [])
        provider_ids = [
            item.get("provider_message_id") for item in snapshot
            if item.get("provider_message_id")
        ]
        source_ids = [
            item.get("message_id") or item.get("job_id") for item in snapshot
            if item.get("message_id") or item.get("job_id")
        ]
        telemetry = {
            "batch_id": turn.get("_id"),
            "job_id": (snapshot[0].get("job_id") if snapshot else None),
            "generation_id": generation_id,
            "conversation_id": turn.get("conversation_id"),
            "lead_id": turn.get("lead_id"),
            "source_message_ids": source_ids,
            "provider_message_ids": provider_ids,
            "pipeline_version": runtime.V2,
            "runtime_generation": turn.get("runtime_generation"),
        }
        metrics["llm_inflight"] = int(metrics.get("llm_inflight", 0)) + 1
        try:
            raw = await asyncio.to_thread(
                process_user_message_sync,
                turn["phone"],
                turn.get("snapshot_text") or "",
                telemetry_context=telemetry,
            )
        except Exception:
            metrics["response_factory_errors"] += 1
            raise
        finally:
            metrics["llm_inflight"] = max(int(metrics.get("llm_inflight", 1)) - 1, 0)

        if not str(raw or "").strip():
            return ""
        lead = await asyncio.to_thread(
            lambda: db["leads"].find_one(
                {"phone": turn["phone"]},
                {"_id": 1, "messages": 1, "prospecto": 1,
                 "conversation_id": 1, "conversation_owner": 1,
                 "human_active": 1},
            ) or {}
        )
        verified_facts: dict[str, Any] = {}
        for message in reversed(lead.get("messages") or []):
            if (
                message.get("role") == "assistant"
                and message.get("batch_id") == turn.get("_id")
                and message.get("generation_id") == generation_id
            ):
                verified_facts = dict(message.get("guardrail_facts") or {})
                break
        latest = snapshot[-1] if snapshot else {}
        admitted = await asyncio.to_thread(
            admit_customer_response,
            candidate=str(raw),
            customer_message=turn.get("snapshot_text") or "",
            lead=lead,
            property_context={
                "property_code": latest.get("property_code"),
                "operation": latest.get("operation"),
            },
            facts=verified_facts,
            source="v2_core",
        )
        if not admitted.get("approved"):
            metrics["final_barrier_suppressed"] += 1
            return ""
        if admitted.get("repaired"):
            metrics["final_barrier_repairs"] = int(
                metrics.get("final_barrier_repairs", 0)
            ) + 1
        return str(admitted.get("response") or "")

    def _sender(phone: str, text: str) -> Mapping[str, Any]:
        from chatbot.whatsapp_client import send_whatsapp_message_detailed_sync

        receipt = send_whatsapp_message_detailed_sync(
            phone, text, traffic_class="customer_reply",
        )
        metrics["provider_sends"] += int(bool(receipt.get("provider_message_id")))
        metrics["provider_unknown"] += int(bool(receipt.get("provider_call_uncertain")))
        return receipt

    try:
        from chatbot.storage import get_db

        db = await asyncio.to_thread(get_db)
        await asyncio.to_thread(queue.ensure_indexes, db)
        _update_status(await asyncio.to_thread(runtime.get_runtime_control, db, create=False))
        while not stop_event.is_set():
            control = await asyncio.to_thread(
                runtime.get_runtime_control, db, create=False,
            )
            _update_status(control)
            result = await process_one_turn(
                db,
                worker_id=worker_id,
                response_factory=_response_factory,
                sender=_sender,
                human_active=lambda: _human_ownership_active(db, current_phone["value"]),
            )
            if result is not None:
                metrics["turns_claimed"] += 1
                metrics["turns_completed"] += int(result.state in queue.TERMINAL_TURN_STATES)
                _update_status(control)
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=max(float(poll_seconds), 0.05),
                )
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        status.update({"status": "stopped", "registered": False,
                       "last_heartbeat": queue.utc_now().isoformat()})
        raise
    except Exception as exc:
        status.update({"status": "error", "health": "FAILED",
                       "registered": False,
                       "last_error": type(exc).__name__,
                       "last_heartbeat": queue.utc_now().isoformat()})
        logger.exception("[CHATBOT_V2_WORKER] worker stopped unexpectedly")
        raise

