"""Signed CRM links bound to one SLA assignment cycle.

These links are intentionally independent from follow-up tasks.  The token is
deterministic for the lead/recipient/cycle tuple and authorization is checked
against Mongo at every open, so closing or replacing the cycle invalidates the
link without a token revocation table.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

from config import Config

from .mongo_identity import mongo_id_variants


TOKEN_VERSION = 1
TOKEN_KIND = "crm_sla_cycle"
LINK_PATH = "/crm/sla-cycle/"
INVALID_LINK_CODE = "LEAD_REASSIGNED_SLA_LOCKED"
SLA_EXPIRED_PENDING_REASSIGNMENT = "SLA_EXPIRED_PENDING_REASSIGNMENT"


class SlaCycleLinkConfigurationError(RuntimeError):
    """The production signing secret is not safely configured."""


class SlaCycleLinkError(ValueError):
    """A signed link cannot be authorized for the current request."""

    def __init__(self, code: str = INVALID_LINK_CODE, status_code: int = 409):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class SlaCycleLinkResolution:
    payload: dict[str, str]
    lead: Mapping[str, Any]
    cycle: Mapping[str, Any]


def _secret() -> bytes:
    try:
        return Config.require_followup_token_secret(version=2).encode("utf-8")
    except RuntimeError as exc:
        # Unit tests and local fixtures may omit Render's dedicated secret.
        # Never use this branch in production; production startup already
        # requires FOLLOWUP_TOKEN_SECRET.
        if not bool(getattr(Config, "IS_PRODUCTION", False)):
            return b"local-only-crm-sla-cycle-link-test-secret"
        raise SlaCycleLinkConfigurationError(
            "sla_cycle_link_signing_secret_invalid"
        ) from exc


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(body: str) -> str:
    digest = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return _encode(digest)


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"sla_cycle_link_{field}_missing")
    return text


def issue_sla_cycle_link_token(
    *, lead_id: Any, recipient_user_id: Any, assignment_cycle_id: Any,
) -> str:
    """Issue a deterministic opaque token for one active-cycle identity."""
    payload = {
        "v": TOKEN_VERSION,
        "kind": TOKEN_KIND,
        "lead_id": _required_text(lead_id, "lead_id"),
        "recipient_user_id": _required_text(recipient_user_id, "recipient_user_id"),
        "assignment_cycle_id": _required_text(assignment_cycle_id, "assignment_cycle_id"),
    }
    body = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_signature(body)}"


def verify_sla_cycle_link_token(token: str) -> dict[str, str]:
    """Verify signature and required binding fields without touching Mongo."""
    raw = str(token or "").strip()
    try:
        body, signature = raw.split(".", 1)
        expected = _signature(body)
        if not hmac.compare_digest(signature, expected):
            raise SlaCycleLinkError(INVALID_LINK_CODE, 403)
        decoded = json.loads(_decode(body).decode("utf-8"))
        if not isinstance(decoded, dict) or decoded.get("kind") != TOKEN_KIND:
            raise SlaCycleLinkError(INVALID_LINK_CODE, 403)
        if int(decoded.get("v") or 0) != TOKEN_VERSION:
            raise SlaCycleLinkError(INVALID_LINK_CODE, 403)
        result = {
            "lead_id": _required_text(decoded.get("lead_id"), "lead_id"),
            "recipient_user_id": _required_text(
                decoded.get("recipient_user_id"), "recipient_user_id"
            ),
            "assignment_cycle_id": _required_text(
                decoded.get("assignment_cycle_id"), "assignment_cycle_id"
            ),
        }
        result["v"] = str(TOKEN_VERSION)
        result["kind"] = TOKEN_KIND
        return result
    except SlaCycleLinkError:
        raise
    except SlaCycleLinkConfigurationError:
        raise
    except Exception as exc:
        raise SlaCycleLinkError(INVALID_LINK_CODE, 403) from exc


def build_sla_cycle_url(
    *, lead_id: Any, recipient_user_id: Any, assignment_cycle_id: Any,
    base_url: str | None = None,
) -> str:
    """Build the only URL allowed in SLA warning/reassignment messages."""
    base = str(base_url or getattr(Config, "CRM_BASE_URL", "")).rstrip("/")
    token = issue_sla_cycle_link_token(
        lead_id=lead_id,
        recipient_user_id=recipient_user_id,
        assignment_cycle_id=assignment_cycle_id,
    )
    return f"{base}{LINK_PATH}{quote(token, safe='')}"


def _same_id(left: Any, right: Any) -> bool:
    return left not in (None, "") and right not in (None, "") and str(left) == str(right)


def _find_lead(db: Any, lead_id: str) -> Mapping[str, Any] | None:
    for candidate in mongo_id_variants(lead_id):
        lead = db["leads"].find_one({"_id": candidate})
        if lead:
            return lead
    return None


def _fail(status_code: int = 409) -> None:
    raise SlaCycleLinkError(INVALID_LINK_CODE, status_code)


def validate_sla_cycle_link(
    db: Any,
    token: str,
    *,
    authenticated_user_id: Any,
) -> SlaCycleLinkResolution:
    """Authorize a link against the live lead, cycle, pointer and owner."""
    payload = verify_sla_cycle_link_token(token)
    recipient_id = str(authenticated_user_id or "").strip()
    if not recipient_id or recipient_id != payload["recipient_user_id"]:
        _fail(403)

    lead = _find_lead(db, payload["lead_id"])
    if not lead:
        _fail(409)

    cycle = db["crm_assignment_cycles"].find_one({
        "assignment_cycle_id": payload["assignment_cycle_id"],
    })
    if not cycle:
        _fail(409)
    if cycle.get("cycle_status") != "active" or cycle.get("unassigned_at") is not None:
        _fail(409)
    if not _same_id(cycle.get("lead_id"), lead.get("_id")):
        _fail(409)
    if str(cycle.get("assigned_to_user_id") or "").strip() != recipient_id:
        _fail(409)

    lifecycle = lead.get("lifecycle") if isinstance(lead.get("lifecycle"), Mapping) else {}
    if str(lifecycle.get("current_assignment_cycle_id") or "").strip() != payload["assignment_cycle_id"]:
        _fail(409)

    # The cycle is canonical; present owner mirrors are checked too when they
    # exist, preventing a stale lead document from authorizing the wrong user.
    owner_values = (
        lifecycle.get("assigned_to_user_id"),
        lead.get("assignment_mirror_owner_user_id"),
        lead.get("assigned_to_user_id"),
    )
    if any(value not in (None, "") and str(value).strip() != recipient_id for value in owner_values):
        _fail(409)

    # The canonical SLA deadline invalidates the link immediately.  This is
    # intentionally checked before the reassignment transaction commits, so
    # the old owner cannot continue operating during scan/transaction delay.
    from .crm_sla_reassignment_worker import canonical_expiration_recheck
    from .crm_metrics import utc_now
    link_check_now = utc_now()
    expiration = canonical_expiration_recheck(cycle, lead, now=link_check_now)
    if expiration.breach_at and link_check_now >= expiration.breach_at:
        raise SlaCycleLinkError(SLA_EXPIRED_PENDING_REASSIGNMENT, 409)

    return SlaCycleLinkResolution(payload=payload, lead=lead, cycle=cycle)


__all__ = [
    "INVALID_LINK_CODE", "SLA_EXPIRED_PENDING_REASSIGNMENT", "LINK_PATH", "SlaCycleLinkConfigurationError",
    "SlaCycleLinkError", "SlaCycleLinkResolution", "build_sla_cycle_url",
    "issue_sla_cycle_link_token", "validate_sla_cycle_link",
    "verify_sla_cycle_link_token",
]
