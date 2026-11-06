"""One-collection price-drop campaign ledger embedded in canonical leads.

Only `leads` is mutated. Prop360 and its price history are read-only.
No sending at import/startup, and the live provider requires explicit gates.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from pymongo import ReturnDocument

from .phone_utils import normalize_phone_strict, phone_identity_variants, has_real_phone
from .price_drop_campaign_classifier import classify_price_drop_reply
from .price_drop_campaign_real import _lead_id, _message

LEADS = "leads"
ROOT = "price_drop_v2.campaigns"
CONTEXT_FLAG = "PRICE_DROP_LEADS_V2_CONTEXT_ENABLED"
SEND_FLAG = "PRICE_DROP_LEADS_V2_SEND_ENABLED"
APPROVED_ID_FLAG = "PRICE_DROP_LEADS_V2_APPROVED_ID"
TEST_PHONE_FLAG = "PRICE_DROP_PILOT_TEST_PHONE"
TEST_NOTICE_FLAG = "PRICE_DROP_LEADS_V2_TEST_NOTICES_ENABLED"
ACTIVE_STATES = ("SENDING", "SENT", "RESPONDED", "OPTED_OUT", "DELIVERY_UNKNOWN")
VALID_CODE = re.compile(r"^\d{4,6}$")
VALID_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{5,79}$")


class PilotBlocked(ValueError):
    pass


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise PilotBlocked("datetime_required")
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _same_phone(lead: dict, phone: str) -> bool:
    actual = lead.get("contact_phone_normalized") or lead.get("phone")
    return normalize_phone_strict(actual or "") == normalize_phone_strict(phone or "")


def _property_price(db, code: str, old: int, new: int) -> datetime:
    prop = db["universo_cartera_prop360"].find_one(
        {"codigo": code},
        {"codigo": 1, "disponible_prop360": 1, "estado.oficina": 1,
         "tipo_operacion.venta": 1, "tipo_operacion.precio_venta.precio_uf": 1},
    ) or {}
    sale = prop.get("tipo_operacion") or {}
    if not (prop.get("disponible_prop360") is True
            and (prop.get("estado") or {}).get("oficina") == "PROCASA SUCRE"
            and sale.get("venta") is True
            and (sale.get("precio_venta") or {}).get("precio_uf") == new
            and old > new > 0):
        raise PilotBlocked("property_price_or_availability_changed")
    change = db["universo_cartera_prop360_historial"].find_one(
        {"codigo": code, "campo": "precio_publicado",
         "moneda_anterior": "UF", "moneda_nueva": "UF",
         "monto_anterior": old, "monto_nuevo": new},
        {"fecha": 1},
        sort=[("fecha", -1)],
    )
    if not change:
        raise PilotBlocked("audited_price_change_missing")
    try:
        when = datetime.fromisoformat(str(change["fecha"]).replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError) as exc:
        raise PilotBlocked("audited_price_date_invalid") from exc
    return utc(when)


def _verified_listing_ids(db, code: str) -> set[str]:
    """Read current and historic publication IDs linked to this exact Prop360 property."""
    prop = db["universo_cartera_prop360"].find_one(
        {"codigo": code}, {"publicaciones": 1},
    ) or {}
    pub = prop.get("publicaciones") or {}
    values: set[str] = set()
    for alias in pub.get("aliases") or []:
        if isinstance(alias, dict) and alias.get("resolvable") is True:
            ext = str(alias.get("external_id") or "").strip()
            if len(ext) >= 7:
                values.add(ext.casefold())
    for key in ("portal_inmobiliario", "yapo", "toctoc", "procasa"):
        portal = pub.get(key) or {}
        for publication in (portal.get("publicaciones") or {}).values():
            if not isinstance(publication, dict):
                continue
            for attr in ("external_id", "code", "code_unique"):
                raw = publication.get(attr)
                entries = raw if isinstance(raw, list) else [raw]
                for candidate in entries:
                    external = str(candidate or "").strip()
                    if len(external) >= 7:
                        values.add(external.casefold())
    return values


def _inquiry_before(lead: dict, code: str, drop_at: datetime,
                    listing_ids: set[str] | None = None) -> bool:
    """Require dated, *property-specific* customer evidence before the drop.

    An unrelated old customer message plus the current prospecto.codigo is
    not evidence of a past inquiry about that listing.
    """
    for message in lead.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") not in ("user", "customer"):
            continue
        text = str(message.get("content") or "")
        if not text.strip():
            continue
        raw_stamp = str(message.get("timestamp") or "")
        try:
            parsed = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        # Chile normally uses NEGATIVE UTC offsets. Check parsed tzinfo,
        # never a '+' substring, or valid Santiago inquiry times disappear.
        if parsed.tzinfo is None:
            continue
        stamp = utc(parsed)
        if stamp >= drop_at:
            continue
        metadata = message.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        keys = ("property_code", "codigo_propiedad", "codigo", "listing_code")
        declared = {str(metadata.get(k) or "").strip() for k in keys}
        has_explicit_code = code in declared
        # Also accept an explicit property code/link in the customer text.
        referenced = re.search(
            r"\b(?:c[oó]d(?:igo)?|propiedad)\s*[:#]?\s*(\d{4,6})\b",
            text, re.I,
        )
        if referenced:
            has_explicit_code = has_explicit_code or referenced.group(1) == code
        if re.search(r"https?://(?:www\.)?procasa\.cl/(?:propiedad/)?"
                     + re.escape(code) + r"(?!\d)", text, re.I):
            has_explicit_code = True
        if not has_explicit_code:
            lowered = text.casefold()
            has_explicit_code = any(
                re.search(r"(?<![a-z0-9])" + re.escape(listing_id)
                          + r"(?![a-z0-9])", lowered)
                for listing_id in (listing_ids or set())
            )
        if has_explicit_code:
            return True
    return False

def preview_one(db, *, lead_id: str, phone: str, campaign_id: str,
                property_code: str, old_price_uf: int, new_price_uf: int,
                test_mode: bool, consent_reviewed: bool = False,
                consent_basis: str = "") -> dict:
    """Read-only preflight. Tests use an explicit existing canonical lead."""
    if not VALID_ID.fullmatch(campaign_id) or not VALID_CODE.fullmatch(property_code):
        raise PilotBlocked("invalid_id_or_property")
    if type(old_price_uf) is not int or type(new_price_uf) is not int:
        raise PilotBlocked("integer_uf_required")
    canonical = normalize_phone_strict(phone)
    if not canonical or not has_real_phone(canonical):
        raise PilotBlocked("invalid_phone")
    lid = _lead_id(lead_id)
    lead = db[LEADS].find_one({"_id": lid})
    if not lead or not _same_phone(lead, canonical):
        raise PilotBlocked("lead_phone_mismatch")
    if lead.get("phone_is_synthetic") or lead.get("contact_identity_incomplete"):
        raise PilotBlocked("incomplete_contact_identity")
    if (lead.get("conversation_status") in {"STOPPED_BY_CLIENT", "STOPPED", "BLOCKED"}
            or (lead.get("price_drop_campaign_v1") or {}).get("opted_out")
            or (lead.get("price_drop_v2") or {}).get("opted_out")):
        raise PilotBlocked("lead_opted_out")
    if test_mode:
        if canonical != normalize_phone_strict(os.getenv(TEST_PHONE_FLAG, "")):
            raise PilotBlocked("test_phone_not_allowlisted")
    else:
        if not consent_reviewed or not str(consent_basis or "").strip():
            raise PilotBlocked("consent_review_required")
        variants = phone_identity_variants(canonical)
        duplicates = list(db[LEADS].find({
            "$or": [
                {"phone": {"$in": variants}},
                {"contact_phone_normalized": {"$in": variants}},
            ],
        }, {"_id": 1}).limit(2))
        if len(duplicates) != 1 or duplicates[0]["_id"] != lid:
            raise PilotBlocked("ambiguous_lead_identity")
    drop_at = _property_price(db, property_code, old_price_uf, new_price_uf)
    if not test_mode and not _inquiry_before(
        lead, property_code, drop_at, _verified_listing_ids(db, property_code)
    ):
        raise PilotBlocked("pre_drop_inquiry_not_verified")
    if any(c.get("campaign_id") == campaign_id for c in
           (lead.get("price_drop_v2") or {}).get("campaigns") or []):
        raise PilotBlocked("campaign_already_recorded")
    if any(c.get("property_code") == property_code
           and c.get("status") in ("PREPARED", "APPROVED", *ACTIVE_STATES)
           for c in (lead.get("price_drop_v2") or {}).get("campaigns") or []):
        raise PilotBlocked("active_campaign_for_property")
    return {
        "lead_id": lid, "phone_normalized": canonical,
        "campaign_id": campaign_id, "property_code": property_code,
        "old_price_uf": old_price_uf, "new_price_uf": new_price_uf,
        "price_drop_at": drop_at,
        "test_mode": bool(test_mode),
        "consent_reviewed": bool(consent_reviewed) if not test_mode else False,
        "consent_basis": str(consent_basis or "")[:180] if not test_mode else "test_only_not_real_consent",
        "simulated_prior_inquiry": bool(test_mode),
        "message": _message(property_code, old_price_uf, new_price_uf),
    }


def prepare_one(db, *, now=None, **data) -> dict:
    """Append exactly one PREPARED campaign to the selected lead."""
    p = preview_one(db, **data)
    stamp = utc(now or datetime.now(timezone.utc))
    row = {k: p[k] for k in (
        "campaign_id", "property_code", "old_price_uf", "new_price_uf",
        "price_drop_at", "test_mode", "consent_reviewed", "consent_basis",
        "simulated_prior_inquiry", "phone_normalized")}
    row.update({
        "status": "PREPARED", "prepared_at": stamp,
        "outbound_message": p["message"], "inbound_provider_ids": [],
        "reply_count": 0, "notifications": {
            "administrator": {"status": "NOT_TRIGGERED"},
            "property_executive": {"status": "NOT_TRIGGERED"},
        },
    })
    result = db[LEADS].update_one(
        {"_id": p["lead_id"],
         f"{ROOT}.campaign_id": {"$ne": p["campaign_id"]},
         ROOT: {"$not": {"$elemMatch": {
             "property_code": p["property_code"],
             "status": {"$in": ["PREPARED", "APPROVED", *ACTIVE_STATES]},
         }}}},
        {"$push": {ROOT: row}},
    )
    if result.modified_count != 1:
        raise PilotBlocked("concurrent_campaign_or_lead_change")
    return {"status": "PREPARED", "campaign_id": p["campaign_id"],
            "lead_id": str(p["lead_id"]), "test_mode": bool(p["test_mode"])}


def approve_one(db, *, lead_id: str, campaign_id: str, operator: str, now=None) -> dict:
    if not str(operator or "").strip():
        raise PilotBlocked("operator_approval_required")
    stamp = utc(now or datetime.now(timezone.utc))
    result = db[LEADS].update_one(
        {"_id": _lead_id(lead_id),
         ROOT: {"$elemMatch": {"campaign_id": campaign_id, "status": "PREPARED"}}},
        {"$set": {f"{ROOT}.$.status": "APPROVED",
                  f"{ROOT}.$.approved_by": str(operator)[:80],
                  f"{ROOT}.$.approved_at": stamp}},
    )
    if result.modified_count != 1:
        raise PilotBlocked("not_prepared_or_already_approved")
    return {"status": "APPROVED", "campaign_id": campaign_id}


def send_one(db, *, lead_id: str, campaign_id: str,
             sender: Callable[[str, str], dict] | None = None, now=None) -> dict:
    """One irreversible attempt; no sender invoked without separate gates."""
    if not (os.getenv(SEND_FLAG, "").lower() in {"true", "1", "yes"}
            and os.getenv(APPROVED_ID_FLAG) == campaign_id):
        return {"status": "DISABLED"}
    if os.getenv(CONTEXT_FLAG, "").lower() not in {"true", "1", "yes"}:
        return {"status": "CONTEXT_DISABLED"}
    lead = db[LEADS].find_one({"_id": _lead_id(lead_id)}) or {}
    c = next((x for x in (lead.get("price_drop_v2") or {}).get("campaigns") or []
              if x.get("campaign_id") == campaign_id), None)
    if not c or c.get("status") != "APPROVED":
        return {"status": "NOT_APPROVED"}
    # Phase 1 is single-recipient acceptance only. Real clients require a
    # reviewed, 3-person audience and dual notification before this gate opens.
    if not c.get("test_mode"):
        return {"status": "REAL_SEND_LOCKED_PENDING_ACCEPTANCE"}
    if normalize_phone_strict(os.getenv(TEST_PHONE_FLAG, "")) != c.get("phone_normalized"):
        return {"status": "TEST_PHONE_NOT_ALLOWLISTED"}
    _property_price(db, c["property_code"], c["old_price_uf"], c["new_price_uf"])
    stamp = utc(now or datetime.now(timezone.utc))
    claimed = db[LEADS].update_one(
        {"_id": lead["_id"], ROOT: {"$elemMatch": {
            "campaign_id": campaign_id, "status": "APPROVED"}}},
        {"$set": {f"{ROOT}.$.status": "SENDING",
                  f"{ROOT}.$.attempted_at": stamp}},
    )
    if claimed.modified_count != 1:
        return {"status": "ALREADY_ATTEMPTED"}
    if sender is None:
        from .whatsapp_client import send_whatsapp_message_detailed_sync
        sender = send_whatsapp_message_detailed_sync
    try:
        receipt = sender(c["phone_normalized"], c["outbound_message"])
    except Exception:
        receipt = {"success": False, "provider_call_uncertain": True}
    accepted = bool(receipt.get("success") and receipt.get("provider_message_id")
                    and not receipt.get("provider_call_uncertain"))
    status = "SENT" if accepted else "DELIVERY_UNKNOWN"
    set_fields = {
        f"{ROOT}.$.status": status,
        f"{ROOT}.$.provider_message_id": receipt.get("provider_message_id"),
        f"{ROOT}.$.provider_status": str(receipt.get("delivery_status") or "unknown")[:40],
    }
    if accepted:
        set_fields[f"{ROOT}.$.sent_at"] = stamp
        set_fields[f"{ROOT}.$.expires_at"] = stamp + timedelta(days=7)
    db[LEADS].update_one(
        {"_id": lead["_id"],
         ROOT: {"$elemMatch": {"campaign_id": campaign_id, "status": "SENDING"}}},
        {"$set": set_fields},
    )
    if accepted:
        # One CRM conversation record, tagged with the campaign reference.
        # An echo from WhatsApp must be de-duplicated by provider_message_id.
        db[LEADS].update_one(
            {"_id": lead["_id"], "messages.provider_message_id": {
                "$ne": receipt["provider_message_id"]}},
            {"$push": {"messages": {
                "role": "assistant", "content": c["outbound_message"],
                "provider_message_id": receipt["provider_message_id"],
                "delivery_status": "accepted",
                "timestamp": stamp.isoformat(),
                "metadata": {"tipo": "price_drop_outreach",
                             "campaign_id": campaign_id,
                             "provider_message_id": receipt["provider_message_id"]},
            }}},
        )
    return {"status": status, "campaign_id": campaign_id,
            "provider_message_id": receipt.get("provider_message_id")}


def active_context(db, *, phone: str, message: str = "", now=None) -> dict | None:
    """Resolve a verified SENT campaign from the canonical lead, no campaign table."""
    if os.getenv(CONTEXT_FLAG, "").lower() not in {"1", "true", "yes"}:
        return None
    canonical = normalize_phone_strict(phone)
    if not canonical:
        return None
    now = utc(now or datetime.now(timezone.utc))
    docs = list(db[LEADS].find(
        {"$or": [{"phone": canonical},
                 {"contact_phone_normalized": canonical}],
         ROOT: {"$elemMatch": {"status": {"$in": ["SENT", "RESPONDED"]},
                               "expires_at": {"$gt": now}}}},
        {"_id": 1, "phone": 1, "contact_phone_normalized": 1, "price_drop_v2.campaigns": 1},
    ).limit(2))
    if len(docs) != 1:
        return None
    lead = docs[0]
    campaign = next((x for x in (lead.get("price_drop_v2") or {}).get("campaigns") or []
                     if x.get("status") in ("SENT", "RESPONDED")
                     and utc(x.get("sent_at")) <= now < utc(x.get("expires_at"))), None)
    if not campaign or not _same_phone(lead, canonical):
        return None
    code = campaign["property_code"]
    # An explicit other-property reference (including a link) must not be
    # attributed to this campaign. Reuse the existing resolver used by V1.
    from .price_drop_pilot import _other_property_referenced
    if _other_property_referenced(message or "", code):
        own_url = f"https://www.procasa.cl/{code}"
        if own_url not in str(message or ""):
            return None
    try:
        _property_price(db, code, campaign["old_price_uf"], campaign["new_price_uf"])
    except PilotBlocked:
        return None
    return {
        "version": "price_drop_leads_v2",
        "campaign_id": campaign["campaign_id"],
        "property_code": code,
        "old_price_uf": float(campaign["old_price_uf"]),
        "new_price_uf": float(campaign["new_price_uf"]),
        "operation": "Venta",
        "outbound_message": campaign["outbound_message"],
        "test_mode": bool(campaign.get("test_mode")),
    }


def record_inbound(db, *, phone: str, provider_message_id: str,
                   text: str, now=None) -> dict:
    """Classify campaign replies directly on the lead without new collections."""
    if not provider_message_id or not text or not text.strip():
        return {"status": "NOT_CAMPAIGN_REPLY"}
    ctx = active_context(db, phone=phone, message=text, now=now)
    if not ctx:
        return {"status": "NOT_CAMPAIGN_REPLY"}
    now = utc(now or datetime.now(timezone.utc))
    label = classify_price_drop_reply(text).as_dict()
    cid = ctx["campaign_id"]
    lead = db[LEADS].find_one(
        {"$or": [{"phone": normalize_phone_strict(phone)},
                 {"contact_phone_normalized": normalize_phone_strict(phone)}],
         ROOT: {"$elemMatch": {"campaign_id": cid, "status": {
             "$in": ["SENT", "RESPONDED"]}}}},
        {"_id": 1, "phone": 1, "contact_phone_normalized": 1,
         "price_drop_v2.campaigns": 1},
    )
    if not lead or not _same_phone(lead, phone):
        return {"status": "NOT_CAMPAIGN_REPLY"}
    # Campaign array entries are append-only; an explicit verified index
    # avoids ambiguous positional updates under concurrent $elemMatch.
    campaign_index = next(
        (i for i, row in enumerate((lead.get("price_drop_v2") or {}).get("campaigns") or [])
         if row.get("campaign_id") == cid), None,
    )
    if campaign_index is None:
        return {"status": "NOT_CAMPAIGN_REPLY"}
    field = f"{ROOT}.{campaign_index}"
    stopped = label["label"] == "BAJA"
    positive = label["label"] in {"INTERES_DECLARADO", "PRIORIDAD_COMERCIAL"}
    update = {
        "$set": {
            f"{field}.status": "OPTED_OUT" if stopped else "RESPONDED",
            f"{field}.last_classification": label,
            f"{field}.last_customer_reply_at": now,
            f"{field}.last_inbound_provider_id": str(provider_message_id),
            f"{field}.last_customer_text": str(text)[:350],
        },
        "$addToSet": {
            "price_drop_v2.processed_inbound_provider_ids": str(provider_message_id),
        },
        "$inc": {f"{field}.reply_count": 1},
    }
    if stopped:
        update["$set"]["price_drop_v2.opted_out"] = True
    if positive:
        stored_campaign = next(
            (row for row in (lead.get("price_drop_v2") or {}).get("campaigns") or []
             if row.get("campaign_id") == cid), {},
        )
        if not stored_campaign.get("has_positive_response"):
            role_status = (
                "PENDING_SIMULATION" if ctx["test_mode"]
                else "PENDING_HUMAN_REVIEW"
            )
            for role in ("administrator", "property_executive"):
                update["$set"][f"{field}.notifications.{role}.status"] = role_status
        update["$set"][f"{field}.has_positive_response"] = True
    result = db[LEADS].update_one(
        {"_id": lead["_id"],
         "price_drop_v2.processed_inbound_provider_ids": {
             "$ne": str(provider_message_id),
         },
         f"{field}.campaign_id": cid,
         f"{field}.status": {"$in": ["SENT", "RESPONDED"]}},
        update,
    )
    if result.modified_count != 1:
        return {"status": "DUPLICATE_IGNORED"}
    return {"status": "ATTRIBUTED", "campaign_id": cid,
            "classification": label["label"], "test_mode": ctx["test_mode"]}


def record_delivery_update(db, payload: dict, *, now=None) -> dict:
    """Track actual delivered/read receipts in leads, not a campaign ledger.

    Provider acceptance is not the same as delivery to a customer's device.
    Unknown/failure callbacks do not imply delivery and cannot auto-resend.
    """
    from .whatsapp_client import normalize_provider_status
    nested = payload.get("data") or {}
    item = nested.get("messages") or nested
    if isinstance(item, list):
        item = item[0] if item else {}
    if not isinstance(item, dict):
        item = {}
    key = item.get("key") or {}
    mid = str(key.get("id") or item.get("messageId")
              or item.get("msgId") or payload.get("messageId") or "").strip()
    if not mid:
        return {"status": "UNMATCHED"}
    raw_status = item.get("status") or nested.get("status") or payload.get("status")
    state = normalize_provider_status(raw_status)
    if state not in {"delivered", "read"}:
        return {"status": "IGNORED"}
    candidates = list(db[LEADS].find(
        {f"{ROOT}.provider_message_id": mid},
        {"_id": 1, "price_drop_v2.campaigns": 1},
    ).limit(2))
    if len(candidates) != 1:
        return {"status": "UNMATCHED_OR_AMBIGUOUS"}
    lead = candidates[0]
    found = [(i, c) for i, c in
             enumerate((lead.get("price_drop_v2") or {}).get("campaigns") or [])
             if c.get("provider_message_id") == mid]
    if len(found) != 1:
        return {"status": "UNMATCHED_OR_AMBIGUOUS"}
    index, campaign = found[0]
    current = campaign.get("provider_status") or "accepted"
    if current == "read" or current == state or (
            current == "delivered" and state == "accepted"):
        return {"status": "UNCHANGED"}
    field = f"{ROOT}.{index}"
    stamp = utc(now or datetime.now(timezone.utc))
    allowed = ["accepted", "sent", "unknown"] if state == "delivered" else [
        "accepted", "sent", "unknown", "delivered"
    ]
    update = {"$set": {f"{field}.provider_status": state,
                       f"{field}.last_provider_update_at": stamp}}
    if state == "delivered":
        update["$set"][f"{field}.delivered_at"] = stamp
    if state == "read":
        update["$set"][f"{field}.read_at"] = stamp
    result = db[LEADS].update_one({
        "_id": lead["_id"], f"{field}.provider_message_id": mid,
        f"{field}.provider_status": {"$in": allowed},
    }, update)
    return {"status": "UPDATED" if result.modified_count else "UNCHANGED"}


def send_test_notice_once(db, *, role: str,
                          sender: Callable[[str, str], dict] | None = None,
                          now=None) -> dict:
    """Send one simulated internal notice to the allowlisted tester only.

    Both roles target the TEST phone, never employee numbers. Each attempted
    provider call is claimed on the same lead before I/O; an uncertain call
    is quarantined rather than retried.
    """
    if os.getenv(TEST_NOTICE_FLAG, "").lower() not in {"1", "true", "yes"}:
        return {"status": "DISABLED"}
    if role not in {"administrator", "property_executive"}:
        return {"status": "INVALID_ROLE"}
    tester = normalize_phone_strict(os.getenv(TEST_PHONE_FLAG, ""))
    if not tester:
        return {"status": "TEST_PHONE_NOT_CONFIGURED"}
    candidates = list(db[LEADS].find({
        "$or": [{"phone": tester}, {"contact_phone_normalized": tester}],
        f"{ROOT}.notifications.{role}.status": "PENDING_SIMULATION",
    }, {"_id": 1, "phone": 1, "contact_phone_normalized": 1,
        "price_drop_v2.campaigns": 1}).limit(2))
    selected = []
    for lead in candidates:
        if not _same_phone(lead, tester):
            continue
        for i, c in enumerate((lead.get("price_drop_v2") or {}).get("campaigns") or []):
            if (c.get("test_mode") is True
                    and c.get("phone_normalized") == tester
                    and c.get("provider_message_id")
                    and c.get("has_positive_response")
                    and c.get("notifications", {}).get(role, {}).get("status") == "PENDING_SIMULATION"):
                selected.append((lead["_id"], i, c))
    if not selected:
        return {"status": "IDLE"}
    # Stable first selection avoids nondeterministic notification order.
    selected.sort(key=lambda row: (str(row[2].get("campaign_id")), str(row[0])))
    lead_id, index, campaign = selected[0]
    field = f"{ROOT}.{index}"
    stamp = utc(now or datetime.now(timezone.utc))
    # Simulated internal alerts share the customer test phone. Do not compete
    # with replies: postpone both notices until the customer has been idle.
    recent_reply = campaign.get("last_customer_reply_at")
    if isinstance(recent_reply, datetime) and stamp - utc(recent_reply) < timedelta(seconds=120):
        return {"status": "DEFERRED_CUSTOMER_ACTIVE"}
    for other_notice in (campaign.get("notifications") or {}).values():
        sent_at = other_notice.get("sent_at") if isinstance(other_notice, dict) else None
        if isinstance(sent_at, datetime) and stamp - utc(sent_at) < timedelta(seconds=90):
            return {"status": "DEFERRED_INTERNAL_THROTTLE"}
    claim = db[LEADS].update_one({
        "_id": lead_id, f"{field}.campaign_id": campaign["campaign_id"],
        f"{field}.test_mode": True,
        f"{field}.notifications.{role}.status": "PENDING_SIMULATION",
    }, {"$set": {
        f"{field}.notifications.{role}.status": "CLAIMED",
        f"{field}.notifications.{role}.attempted_at": stamp,
    }})
    if claim.modified_count != 1:
        return {"status": "ALREADY_CLAIMED"}
    role_name = "ADMINISTRADOR" if role == "administrator" else "EJECUTIVO DE LA PROPIEDAD"
    old_text = f"{campaign['old_price_uf']:,}".replace(",", ".")
    new_text = f"{campaign['new_price_uf']:,}".replace(",", ".")
    from .lead_router import build_secure_crm_url
    crm_link = build_secure_crm_url({"_id": lead_id})
    message = (
        f"PRUEBA INTERNA — AVISO SIMULADO AL {role_name}\n\n"
        "🔔 PROCASA — Interesado en rebaja de precio\n"
        f"🏠 Propiedad: {campaign['property_code']}\n"
        f"📉 UF {old_text} → UF {new_text}\n"
        f"📱 Cliente de prueba: {tester}\n"
        f"💬 Respuesta: {str(campaign.get('last_customer_text') or 'Interés registrado')[:160]}\n"
        f"🔗 Abrir lead en CRM: {crm_link}\n"
        "Aviso de prueba: no se notificó a un ejecutivo real."
    )
    if sender is None:
        from .whatsapp_client import send_whatsapp_message_detailed_sync
        sender = send_whatsapp_message_detailed_sync
    try:
        receipt = sender(tester, message)
    except Exception:
        receipt = {"success": False, "provider_call_uncertain": True}
    accepted = bool(receipt.get("success") and receipt.get("provider_message_id")
                    and not receipt.get("provider_call_uncertain"))
    status = "SENT" if accepted else "DELIVERY_UNKNOWN"
    db[LEADS].update_one({
        "_id": lead_id, f"{field}.campaign_id": campaign["campaign_id"],
        f"{field}.notifications.{role}.status": "CLAIMED",
    }, {"$set": {
        f"{field}.notifications.{role}.status": status,
        f"{field}.notifications.{role}.provider_message_id": receipt.get("provider_message_id"),
        f"{field}.notifications.{role}.completed_at": stamp,
        f"{field}.notifications.{role}.sent_to": "TEST_PHONE_ONLY",
        **({f"{field}.notifications.{role}.sent_at": stamp} if accepted else {}),
    }})
    return {"status": status, "campaign_id": campaign["campaign_id"],
            "role": role, "recipient_last4": tester[-4:]}
