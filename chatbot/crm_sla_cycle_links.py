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
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote

from config import Config

from .mongo_identity import mongo_id_variants


TOKEN_VERSION = 1
TOKEN_KIND = "crm_sla_cycle"
LINK_PATH = "/crm/sla-cycle/"
SHORT_LINK_COLLECTION = "crm_sla_short_links_v1"
SHORT_LINK_PATH = "/crm/s/"
SHORT_LINK_VERSION = 1
SHORT_ID_BYTES = 12
SHORT_ID_LENGTH = 16
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


def issue_sla_short_id(
    *, lead_id: Any, recipient_user_id: Any, assignment_cycle_id: Any,
) -> str:
    """Return one opaque, deterministic reference for one cycle identity.

    The HMAC keeps the reference non-reversible while determinism makes
    notification retries reuse the same server-side permission record.
    Twelve digest bytes provide 96 bits of entropy in a 16-character URL-safe
    reference; no lead, user, or cycle identifier is encoded in it.
    """
    identity = "|".join((
        str(SHORT_LINK_VERSION),
        _required_text(lead_id, "lead_id"),
        _required_text(recipient_user_id, "recipient_user_id"),
        _required_text(assignment_cycle_id, "assignment_cycle_id"),
    ))
    digest = hmac.new(
        _secret(),
        f"crm-sla-short-link-v{SHORT_LINK_VERSION}|{identity}".encode("utf-8"),
        hashlib.sha256,
    ).digest()[:SHORT_ID_BYTES]
    return _encode(digest)


def build_sla_short_url(
    *, lead_id: Any, recipient_user_id: Any, assignment_cycle_id: Any,
    base_url: str | None = None,
) -> str:
    """Build the opaque URL used by new SLA reassignment messages."""
    base = str(base_url or getattr(Config, "CRM_BASE_URL", "")).rstrip("/")
    short_id = issue_sla_short_id(
        lead_id=lead_id,
        recipient_user_id=recipient_user_id,
        assignment_cycle_id=assignment_cycle_id,
    )
    return f"{base}{SHORT_LINK_PATH}{quote(short_id, safe='')}"


def ensure_sla_short_link(
    db: Any,
    *, lead_id: Any, recipient_user_id: Any, assignment_cycle_id: Any,
    policy_version: str = "crm_sla_reassignment_v1",
) -> str:
    """Persist/reuse the short-link identity without granting access itself.

    Mongo's unique ``_id`` is the idempotency key.  Authorization is never
    decided from this document alone; validation below reconstructs the
    canonical signed identity and runs the existing cycle/owner checks.
    """
    short_id = issue_sla_short_id(
        lead_id=lead_id,
        recipient_user_id=recipient_user_id,
        assignment_cycle_id=assignment_cycle_id,
    )
    now = datetime.now(timezone.utc)
    db[SHORT_LINK_COLLECTION].update_one(
        {"_id": short_id},
        {"$setOnInsert": {
            "_id": short_id,
            "lead_id": str(lead_id),
            "recipient_user_id": str(recipient_user_id),
            "assignment_cycle_id": str(assignment_cycle_id),
            "created_at": now,
            "policy_version": str(policy_version),
        }},
        upsert=True,
    )
    return short_id


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

    # A deadline is historical SLA/KPI state only.  The signed link remains
    # valid until the cycle is atomically closed or the lead's current-cycle
    # pointer/owner changes.  This keeps the access, link and management gate
    # semantics aligned during the scanner/transaction race window.

    return SlaCycleLinkResolution(payload=payload, lead=lead, cycle=cycle)


def validate_sla_short_link(
    db: Any,
    short_id: str,
    *,
    authenticated_user_id: Any,
) -> SlaCycleLinkResolution:
    """Resolve an opaque reference and reuse canonical cycle authorization."""
    raw_short_id = str(short_id or "").strip()
    if len(raw_short_id) != SHORT_ID_LENGTH or any(
        char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
        for char in raw_short_id
    ):
        _fail(403)

    record = db[SHORT_LINK_COLLECTION].find_one({"_id": raw_short_id})
    if not record:
        _fail(403)

    # The short record is only an opaque lookup.  Rebuild the canonical
    # signed identity and run the existing Mongo-backed owner/cycle/pointer
    # validation so the short path cannot become an authorization bypass.
    token = issue_sla_cycle_link_token(
        lead_id=record.get("lead_id"),
        recipient_user_id=record.get("recipient_user_id"),
        assignment_cycle_id=record.get("assignment_cycle_id"),
    )
    return validate_sla_cycle_link(
        db,
        token,
        authenticated_user_id=authenticated_user_id,
    )


__all__ = [
    "INVALID_LINK_CODE", "SLA_EXPIRED_PENDING_REASSIGNMENT", "LINK_PATH",
    "SHORT_LINK_COLLECTION", "SHORT_LINK_PATH", "SHORT_LINK_VERSION",
    "SHORT_ID_LENGTH", "SlaCycleLinkConfigurationError",
    "SlaCycleLinkError", "SlaCycleLinkResolution", "build_sla_cycle_url",
    "build_sla_short_url", "ensure_sla_short_link", "issue_sla_short_id",
    "issue_sla_cycle_link_token", "validate_sla_cycle_link", "validate_sla_short_link",
    "verify_sla_cycle_link_token",
]
