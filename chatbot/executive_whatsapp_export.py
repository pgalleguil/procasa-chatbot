"""Offline parser for authorized WhatsApp .txt chat exports (NOT an importer).

No MongoDB access and no outbound API. This produces verification-required
records only: a .txt export does not prove the phone or the assignment cycle.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .phone_utils import has_real_phone, normalize_phone_strict

MAX_EXPORT_CHARACTERS = 2_000_000
MAX_EXPORT_MESSAGES = 5_000

# Supports [11/11/26, 09:15:20] Nombre: Mensaje and
# 11-11-2026 09:15 - Nombre: Mensaje, as well as multiline messages.
_EXPORT_LINE = re.compile(
    r"^\[?"
    r"(?P<day>\d{1,2})[/-](?P<month>\d{1,2})[/-](?P<year>\d{2}|\d{4})"
    r",?\s+"
    r"(?P<clock>\d{1,2}:\d{2}(?::\d{2})?)"
    r"(?:\s*(?P<meridiem>[AaPp]\.?\s*[Mm]\.?))?"
    r"\]?\s*(?:-\s*)?"
    r"(?P<sender>[^:\r\n]{1,100}):\s*(?P<text>.*)$"
)


@dataclass(frozen=True)
class ExportBinding:
    """Metadata verified OUTSIDE the export; labels alone are untrusted."""

    lead_id: str
    executive_user_id: str
    executive_label: str
    client_label: str
    client_phone: str
    session_id: str
    tz_name: str = "America/Santiago"

    def __post_init__(self) -> None:
        if not all(
            isinstance(x, str) and x.strip()
            for x in (
                self.lead_id, self.executive_user_id, self.executive_label,
                self.client_label, self.client_phone, self.session_id,
                self.tz_name,
            )
        ):
            raise ValueError("incomplete_historical_export_binding")
        if self.executive_label.strip().casefold() == self.client_label.strip().casefold():
            raise ValueError("ambiguous_historical_participant_labels")
        phone = normalize_phone_strict(self.client_phone)
        if not phone or not has_real_phone(phone):
            raise ValueError("invalid_historical_client_phone")
        try:
            ZoneInfo(self.tz_name)
        except (KeyError, ValueError):
            raise ValueError("invalid_export_timezone") from None


def _parse_datetime(parts: dict[str, str], local_tz: ZoneInfo) -> datetime | None:
    try:
        year_str = parts["year"]
        year = int(year_str)
        if len(year_str) == 2:
            year += 2000
        hour, minute, *seconds = map(int, parts["clock"].split(":"))
        second = seconds[0] if seconds else 0
        meridiem = re.sub(r"\s|\.", "", parts.get("meridiem") or "").upper()
        if meridiem:
            if hour < 1 or hour > 12:
                return None
            hour = (hour % 12) + (12 if meridiem.startswith("P") else 0)
        raw = datetime(
            year, int(parts["month"]), int(parts["day"]),
            hour, minute, second,
        )
        # Reject ambiguous/nonexistent DST local times rather than
        # inventing an offset for a commercial-audit timestamp.
        a = raw.replace(tzinfo=local_tz, fold=0)
        b = raw.replace(tzinfo=local_tz, fold=1)
        if a.utcoffset() != b.utcoffset():
            return None
        utc = a.astimezone(timezone.utc)
        if utc.astimezone(local_tz).replace(tzinfo=None) != raw:
            return None
        return utc
    except (TypeError, ValueError, OverflowError):
        return None


def parse_authorized_whatsapp_export(
    source_text: str,
    *,
    binding: ExportBinding,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[str, Any]:
    """Parse ONLY; never persist or infer a verified assignment cycle.

    Both period bounds must be UTC-aware. It returns extracted content for
    explicit human review, not a database migration or campaign eligibility.
    """
    if not isinstance(source_text, str) or len(source_text) > MAX_EXPORT_CHARACTERS:
        raise ValueError("invalid_or_oversized_historical_export")
    if not isinstance(binding, ExportBinding):
        raise ValueError("historical_export_binding_required")
    if any(
        not isinstance(x, datetime) or x.tzinfo is None or x.utcoffset() is None
        for x in (start_utc, end_utc)
    ):
        raise ValueError("historical_export_window_requires_timezone")
    lo, hi = start_utc.astimezone(timezone.utc), end_utc.astimezone(timezone.utc)
    if lo > hi:
        raise ValueError("reversed_historical_export_window")
    # Do not search 3rd-party contacts by sender names or guess unknown aliases.
    speakers = {
        binding.executive_label.strip().casefold(): "executive",
        binding.client_label.strip().casefold(): "client",
    }
    tz = ZoneInfo(binding.tz_name)
    messages: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    pending: dict[str, Any] | None = None
    occurrence: Counter[str] = Counter()

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        content = pending["content"].strip()
        if not content:
            skipped["empty_text"] += 1
        elif len(content) > 20_000:
            skipped["oversized_message"] += 1
        elif pending["actor_type"] not in ("client", "executive"):
            skipped["unrecognized_sender"] += 1
        elif pending["timestamp"] is None:
            skipped["invalid_or_ambiguous_timestamp"] += 1
        elif not (lo <= pending["timestamp"] <= hi):
            skipped["outside_requested_window"] += 1
        elif len(messages) >= MAX_EXPORT_MESSAGES:
            skipped["max_export_messages_exceeded"] += 1
        else:
            seed = "|".join((
                binding.session_id, binding.lead_id,
                pending["timestamp"].isoformat(),
                pending["actor_type"], content,
            ))
            occurrence[seed] += 1
            digest = hashlib.sha256(
                f"{seed}|{occurrence[seed]}".encode("utf-8")
            ).hexdigest()
            messages.append({
                "evidence_key": f"whatsapp-export:{digest}",
                "lead_id": binding.lead_id,
                "session_id": binding.session_id,
                "executive_user_id": binding.executive_user_id,
                "client_phone": normalize_phone_strict(binding.client_phone),
                "actor_type": pending["actor_type"],
                "content": content,
                "timestamp": pending["timestamp"],
                "ingestion_source": "whatsapp_export_txt",
                "source_verified": False,
                "assignment_cycle_id": None,
            })
        pending = None

    for line in source_text.replace("\ufeff", "").splitlines():
        match = _EXPORT_LINE.match(line)
        if match:
            flush()
            fields = match.groupdict()
            pending = {
                "actor_type": speakers.get(fields["sender"].strip().casefold()),
                "timestamp": _parse_datetime(fields, tz),
                "content": fields["text"],
            }
        elif pending is not None:
            pending["content"] += "\n" + line
        elif line.strip():
            skipped["unrecognized_header_or_system_line"] += 1
    flush()
    return {
        "records": messages,
        "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "skipped_counts": dict(skipped),
        "coverage_complete": False,
        "requires_phone_and_assignment_verification": True,
        "persisted": False,
        "outbound_messages_sent": 0,
    }
