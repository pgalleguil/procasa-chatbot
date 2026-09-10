"""Audited persistence boundary for immutable pricing snapshots.

The repository is deliberately narrow: it can write only the two V1
collections declared below, never operational collections. Snapshot writes
are insert-only and require explicit confirmation from the CLI.
"""

from __future__ import annotations

import copy
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from pymongo.errors import BulkWriteError, DuplicateKeyError

from .models import PropertyDailySnapshotV1, snapshot_document_id
from .snapshot_builder import PropertySnapshotBuilder, SnapshotBuildResult
from .time_utils import ensure_aware, to_business_time, to_utc


SNAPSHOT_COLLECTION = "pricing_intelligence_property_snapshots_v1"
RUN_COLLECTION = "pricing_intelligence_snapshot_runs_v1"
# Compatibility aliases retained for the Phase 2A report/tests.
PROPOSED_COLLECTION = SNAPSHOT_COLLECTION
PROPOSED_IDEMPOTENT_INDEX = (("property_code", 1), ("snapshot_date_local", 1))
SNAPSHOT_SCHEMA_VERSION = "PropertyDailySnapshotV1"
RUN_SCHEMA_VERSION = "PricingIntelligenceSnapshotRunV1"
RUN_STATUSES = frozenset({"RUNNING", "COMPLETED", "PARTIAL", "FAILED"})
ALREADY_COMPLETE = "ALREADY_COMPLETE"
INCONSISTENT_SNAPSHOT_STATE = "INCONSISTENT_SNAPSHOT_STATE"
SOURCE_COUNT_ANOMALY = "SOURCE_COUNT_ANOMALY"
DEFAULT_BATCH_SIZE = 250

_PII_KEY_NAMES = frozenset(
    {
        "email",
        "phone",
        "telefono",
        "rut",
        "owner_name",
        "propietario_nombre",
        "nombre_propietario",
        "message",
        "messages",
        "direccion",
        "street_address",
        "user_agent",
        "ip",
    }
)
_SAFE_RUN_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


class PersistenceDisabled(RuntimeError):
    """Raised when persistence is attempted without explicit confirmation."""


class PersistenceError(RuntimeError):
    """Raised when immutable snapshot invariants cannot be satisfied."""


class SnapshotPIIError(PersistenceError):
    """Raised when a snapshot payload contains a prohibited key."""


@dataclass(frozen=True)
class PersistenceResult:
    status: str
    run_id: Optional[str]
    snapshot_date_local: str
    as_of_local: str
    as_of_utc: str
    expected_properties: int
    attempted_properties: int
    inserted_snapshots: int
    skipped_existing: int
    failed_snapshots: int
    error_summary: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "snapshot_date_local": self.snapshot_date_local,
            "as_of_local": self.as_of_local,
            "as_of_utc": self.as_of_utc,
            "expected_properties": self.expected_properties,
            "attempted_properties": self.attempted_properties,
            "inserted_snapshots": self.inserted_snapshots,
            "skipped_existing": self.skipped_existing,
            "failed_snapshots": self.failed_snapshots,
            "error_summary": list(self.error_summary),
        }


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return ensure_aware(value).isoformat()
    return str(value)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normal_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value)).casefold()
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _is_prohibited_key(key: Any) -> bool:
    normalized = _normal_key(key)
    if normalized in _PII_KEY_NAMES:
        return True
    if "ip" in set(normalized.split("_")):
        return True
    return any(
        normalized.startswith(f"{name}_") or normalized.endswith(f"_{name}")
        for name in _PII_KEY_NAMES
        if "_" in name
    )


def validate_no_pii_payload(payload: Any, *, path: str = "$") -> None:
    """Reject prohibited keys at any depth without inspecting/storing values."""

    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if _is_prohibited_key(key):
                raise SnapshotPIIError(f"prohibited snapshot key at {path}.{key}")
            validate_no_pii_payload(value, path=f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            validate_no_pii_payload(value, path=f"{path}[{index}]")


def _parse_date(value: Any) -> str:
    if isinstance(value, datetime):
        value = to_business_time(value).date()
    if isinstance(value, date):
        return value.isoformat()
    text = unicodedata.normalize("NFKC", str(value)).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise PersistenceError("snapshot_date_local inválida") from exc


def _parse_cutoff(value: Any) -> datetime:
    if isinstance(value, datetime):
        # Mongo clients without tz_aware return UTC datetimes as naive. The
        # run ledger contract stores these fields as UTC, so normalise that
        # representation without guessing a business-local timezone.
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return ensure_aware(value)
    if not isinstance(value, str):
        raise PersistenceError("cutoff inválido")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PersistenceError("cutoff inválido") from exc
    return ensure_aware(parsed)


def _content_without_identity(document: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(document))
    value.pop("_id", None)
    value.pop("run_id", None)
    return value


def _source_count(source_counts: Mapping[str, Any], key: str) -> Optional[int]:
    value = source_counts.get(key)
    if isinstance(value, Mapping):
        value = value.get("count")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class SnapshotRepository:
    def __init__(
        self,
        builder: PropertySnapshotBuilder,
        db: Any = None,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        now_fn: Any = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size debe ser mayor que cero")
        self.builder = builder
        self.db = db
        self.batch_size = batch_size
        self.now_fn = now_fn or _utc_now

    def build(
        self,
        properties: Iterable[Mapping[str, Any]],
        *,
        as_of=None,
        separate_price_events: Iterable[Mapping[str, Any]] = (),
        property_code: Optional[str] = None,
    ) -> SnapshotBuildResult:
        return self.builder.build(
            properties,
            as_of=as_of,
            separate_price_events=separate_price_events,
            property_code=property_code,
        )

    def _require_db(self) -> Any:
        if self.db is None:
            raise PersistenceError("MongoDB no está configurado para persistencia")
        return self.db

    @property
    def snapshot_collection(self) -> Any:
        return self._require_db()[SNAPSHOT_COLLECTION]

    @property
    def run_collection(self) -> Any:
        return self._require_db()[RUN_COLLECTION]

    def ensure_indexes(self) -> dict[str, list[str]]:
        """Create only the documented indexes on the two new collections."""

        snapshot_indexes = [
            self.snapshot_collection.create_index(
                "snapshot_date_local", name="snapshot_date_local_idx"
            ),
            self.snapshot_collection.create_index(
                list(PROPOSED_IDEMPOTENT_INDEX), name="property_code_snapshot_date_idx"
            ),
        ]
        run_indexes = [
            self.run_collection.create_index("snapshot_date_local", name="snapshot_date_local_idx"),
            self.run_collection.create_index("status", name="status_idx"),
        ]
        return {SNAPSHOT_COLLECTION: snapshot_indexes, RUN_COLLECTION: run_indexes}

    def get_snapshot_for_date(self, snapshot_date_local: date | str) -> list[dict[str, Any]]:
        return list(self.snapshot_collection.find({"snapshot_date_local": _parse_date(snapshot_date_local)}))

    def get_existing_property_codes(self, snapshot_date_local: date | str) -> set[str]:
        return {
            str(document["property_code"])
            for document in self.get_snapshot_for_date(snapshot_date_local)
            if document.get("property_code")
        }

    def get_incomplete_run(self, snapshot_date_local: date | str) -> Optional[dict[str, Any]]:
        return self.run_collection.find_one(
            {
                "snapshot_date_local": _parse_date(snapshot_date_local),
                "status": {"$in": ["RUNNING", "PARTIAL"]},
            },
            sort=[("started_at_utc", -1)],
        )

    def get_latest_completed_run(self, *, before_snapshot_date: date | str | None = None) -> Optional[dict[str, Any]]:
        query: dict[str, Any] = {"status": "COMPLETED"}
        if before_snapshot_date is not None:
            query["snapshot_date_local"] = {"$lt": _parse_date(before_snapshot_date)}
        return self.run_collection.find_one(query, sort=[("snapshot_date_local", -1), ("started_at_utc", -1)])

    def create_run(self, run: Mapping[str, Any]) -> str:
        document = dict(run)
        run_id = str(document.get("run_id") or uuid.uuid4())
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise PersistenceError("run_id inválido")
        if document.get("status") not in RUN_STATUSES:
            raise PersistenceError("status de run inválido")
        document["run_id"] = run_id
        document["_id"] = run_id
        self.run_collection.insert_one(document)
        return run_id

    def _update_run(self, run_id: str, updates: Mapping[str, Any]) -> None:
        self.run_collection.update_one({"_id": run_id}, {"$set": dict(updates)})

    def complete_run(self, run_id: str, *, completed_at_utc: Optional[datetime] = None, **updates: Any) -> None:
        self._update_run(
            run_id,
            {**updates, "status": "COMPLETED", "completed_at_utc": completed_at_utc or self.now_fn()},
        )

    def mark_partial(self, run_id: str, *, error_summary: Iterable[str] = (), **updates: Any) -> None:
        self._update_run(run_id, {**updates, "status": "PARTIAL", "error_summary": list(error_summary)})

    def mark_failed(self, run_id: str, *, error_summary: Iterable[str] = (), **updates: Any) -> None:
        self._update_run(
            run_id,
            {
                **updates,
                "status": "FAILED",
                "completed_at_utc": self.now_fn(),
                "error_summary": list(error_summary),
            },
        )

    @staticmethod
    def _document_for_snapshot(snapshot: PropertyDailySnapshotV1, run_id: str) -> dict[str, Any]:
        document = snapshot.to_dict()
        expected_id = snapshot_document_id(snapshot.snapshot_date_local, snapshot.property_code)
        if document.get("_id") != expected_id:
            raise PersistenceError("identidad determinística de snapshot inválida")
        document["run_id"] = run_id
        validate_no_pii_payload(document)
        return document

    def _validate_existing(self, existing: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
        if existing.get("_id") != candidate["_id"]:
            raise PersistenceError("snapshot existente con _id inconsistente")
        if existing.get("snapshot_date_local") != candidate.get("snapshot_date_local"):
            raise PersistenceError("INCONSISTENT_SNAPSHOT_STATE: fecha existente distinta")
        if existing.get("property_code") != candidate.get("property_code"):
            raise PersistenceError("INCONSISTENT_SNAPSHOT_STATE: código existente distinto")
        if existing.get("schema_version") != candidate.get("schema_version"):
            raise PersistenceError("INCONSISTENT_SNAPSHOT_STATE: schema existente distinto")
        if _content_without_identity(existing) != _content_without_identity(candidate):
            raise PersistenceError("INCONSISTENT_SNAPSHOT_STATE: contenido distinto para _id existente")

    def _insert_missing(self, documents: list[dict[str, Any]]) -> tuple[int, tuple[str, ...]]:
        inserted = 0
        errors: list[str] = []
        for start in range(0, len(documents), self.batch_size):
            batch = documents[start : start + self.batch_size]
            try:
                result = self.snapshot_collection.insert_many(batch, ordered=False)
                inserted += len(result.inserted_ids)
            except (BulkWriteError, DuplicateKeyError) as exc:
                details = getattr(exc, "details", {}) or {}
                inserted += int(details.get("nInserted", 0) or 0)
                duplicate_ids: set[Any] = set()
                for item in details.get("writeErrors", []):
                    if item.get("code") == 11000 and 0 <= item.get("index", -1) < len(batch):
                        duplicate_ids.add(batch[item["index"]]["_id"])
                for document in batch:
                    existing = self.snapshot_collection.find_one({"_id": document["_id"]})
                    if existing is not None and document["_id"] in duplicate_ids:
                        self._validate_existing(existing, document)
                errors.append(f"batch_insert_error:{type(exc).__name__}")
        return inserted, tuple(errors)

    @staticmethod
    def _make_result(
        *,
        status: str,
        run_id: Optional[str],
        snapshot_date_local: str,
        as_of_local: datetime,
        as_of_utc: datetime,
        expected_properties: int,
        attempted_properties: int,
        inserted_snapshots: int,
        skipped_existing: int,
        failed_snapshots: int,
        error_summary: Iterable[str] = (),
    ) -> PersistenceResult:
        return PersistenceResult(
            status=status,
            run_id=run_id,
            snapshot_date_local=snapshot_date_local,
            as_of_local=_iso(as_of_local),
            as_of_utc=_iso(as_of_utc),
            expected_properties=expected_properties,
            attempted_properties=attempted_properties,
            inserted_snapshots=inserted_snapshots,
            skipped_existing=skipped_existing,
            failed_snapshots=failed_snapshots,
            error_summary=tuple(error_summary),
        )

    def persist(
        self,
        snapshots: Iterable[PropertyDailySnapshotV1],
        *,
        confirm_production_write: bool = False,
        run_id: Optional[str] = None,
        builder_version: Optional[str] = None,
        source_counts: Mapping[str, Any] | None = None,
        linkage_metrics: Mapping[str, Any] | None = None,
        quality_metrics: Mapping[str, Any] | None = None,
        error_summary: Iterable[str] = (),
        resumed_from: Optional[str] = None,
        expected_properties: Optional[int] = None,
    ) -> PersistenceResult:
        """Persist a complete prospective daily set with insert-only semantics."""

        if not confirm_production_write:
            raise PersistenceDisabled(
                "persistencia bloqueada: se requiere --persist --confirm-production-write"
            )
        self._require_db()
        snapshot_list = list(snapshots)
        if not snapshot_list:
            raise PersistenceError("no hay snapshots para persistir")

        snapshot_date = _parse_date(snapshot_list[0].snapshot_date_local)
        as_of_local = to_business_time(snapshot_list[0].as_of_local)
        as_of_utc = to_utc(snapshot_list[0].as_of_utc)
        if as_of_local.date().isoformat() != snapshot_date:
            raise PersistenceError("snapshot_date_local no coincide con as_of_local")
        if to_utc(as_of_local) != as_of_utc:
            raise PersistenceError("as_of_local y as_of_utc no representan el mismo cutoff")

        source_counts = dict(source_counts or {})
        linkage_metrics = dict(linkage_metrics or {})
        quality_metrics = dict(quality_metrics or {})
        requested_run_id = run_id or str(uuid.uuid4())
        if not _SAFE_RUN_ID.fullmatch(requested_run_id):
            raise PersistenceError("run_id inválido")
        documents = []
        seen_ids: set[str] = set()
        for snapshot in snapshot_list:
            if snapshot.snapshot_date_local.isoformat() != snapshot_date:
                raise PersistenceError("todos los snapshots deben compartir snapshot_date_local")
            if to_business_time(snapshot.as_of_local) != as_of_local or to_utc(snapshot.as_of_utc) != as_of_utc:
                raise PersistenceError("todos los snapshots deben compartir el cutoff del run")
            document = self._document_for_snapshot(snapshot, requested_run_id)
            if document["_id"] in seen_ids:
                raise PersistenceError("código de propiedad duplicado en el lote")
            seen_ids.add(document["_id"])
            documents.append(document)

        expected_properties = int(expected_properties or len(documents))
        if expected_properties < len(documents):
            raise PersistenceError("expected_properties no puede ser menor que el lote")
        prior_completed = self.get_latest_completed_run(before_snapshot_date=snapshot_date)
        master_count = _source_count(source_counts, "master_properties")
        if prior_completed is not None and master_count is not None:
            previous_count = int(prior_completed.get("expected_properties") or 0)
            if previous_count and abs(master_count - previous_count) / previous_count > 0.20:
                self.ensure_indexes()
                self.create_run(
                    {
                        "run_id": requested_run_id,
                        "schema_version": RUN_SCHEMA_VERSION,
                        "builder_version": builder_version or "unknown",
                        "status": "FAILED",
                        "started_at_utc": self.now_fn(),
                        "completed_at_utc": self.now_fn(),
                        "as_of_local": as_of_local.isoformat(),
                        "as_of_utc": as_of_utc.isoformat(),
                        "snapshot_date_local": snapshot_date,
                        "expected_properties": expected_properties,
                        "attempted_properties": 0,
                        "inserted_snapshots": 0,
                        "skipped_existing": 0,
                        "failed_snapshots": 0,
                        "source_counts": source_counts,
                        "linkage_metrics": linkage_metrics,
                        "quality_metrics": quality_metrics,
                        "error_summary": [SOURCE_COUNT_ANOMALY],
                        "resumed_from": resumed_from,
                    }
                )
                raise PersistenceError(SOURCE_COUNT_ANOMALY)

        self.ensure_indexes()
        existing_run = self.get_incomplete_run(snapshot_date)
        existing_documents = {
            document["_id"]: document
            for document in self.get_snapshot_for_date(snapshot_date)
            if document.get("_id")
        }
        complete_run = self.run_collection.find_one(
            {"snapshot_date_local": snapshot_date, "status": "COMPLETED"},
            sort=[("completed_at_utc", -1)],
        )
        if complete_run is not None:
            if len(existing_documents) != int(complete_run.get("expected_properties") or 0):
                raise PersistenceError(INCONSISTENT_SNAPSHOT_STATE)
            return self._make_result(
                status=ALREADY_COMPLETE,
                run_id=str(complete_run.get("run_id") or complete_run.get("_id")),
                snapshot_date_local=snapshot_date,
                as_of_local=_parse_cutoff(complete_run["as_of_local"]),
                as_of_utc=_parse_cutoff(complete_run["as_of_utc"]),
                expected_properties=int(complete_run.get("expected_properties") or expected_properties),
                attempted_properties=0,
                inserted_snapshots=0,
                skipped_existing=len(existing_documents),
                failed_snapshots=0,
            )

        if existing_run is None and existing_documents:
            raise PersistenceError(INCONSISTENT_SNAPSHOT_STATE)

        effective_run_id = str(existing_run.get("run_id") or existing_run.get("_id")) if existing_run else requested_run_id
        if existing_run is not None:
            original_local = _parse_cutoff(existing_run["as_of_local"])
            original_utc = _parse_cutoff(existing_run["as_of_utc"])
            if to_business_time(original_local) != as_of_local or to_utc(original_utc) != as_of_utc:
                raise PersistenceError("INCONSISTENT_SNAPSHOT_STATE: cutoff distinto al run parcial")
            expected_properties = int(existing_run.get("expected_properties") or expected_properties)
        else:
            self.create_run(
                {
                    "run_id": effective_run_id,
                    "schema_version": RUN_SCHEMA_VERSION,
                    "builder_version": builder_version or "unknown",
                    "status": "RUNNING",
                    "started_at_utc": self.now_fn(),
                    "completed_at_utc": None,
                    "as_of_local": as_of_local.isoformat(),
                    "as_of_utc": as_of_utc.isoformat(),
                    "snapshot_date_local": snapshot_date,
                    "expected_properties": expected_properties,
                    "attempted_properties": 0,
                    "inserted_snapshots": 0,
                    "skipped_existing": 0,
                    "failed_snapshots": 0,
                    "source_counts": source_counts,
                    "linkage_metrics": linkage_metrics,
                    "quality_metrics": quality_metrics,
                    "error_summary": list(error_summary),
                    "resumed_from": resumed_from,
                }
            )

        missing: list[dict[str, Any]] = []
        skipped = 0
        for document in documents:
            existing = existing_documents.get(document["_id"])
            if existing is None:
                missing.append(document)
            else:
                self._validate_existing(existing, document)
                skipped += 1

        inserted, insert_errors = self._insert_missing(missing)
        failed = len(insert_errors)
        attempted = len(documents)
        common_updates = {
            "attempted_properties": attempted,
            "inserted_snapshots": inserted,
            "skipped_existing": skipped,
            "failed_snapshots": failed,
            "source_counts": source_counts,
            "linkage_metrics": linkage_metrics,
            "quality_metrics": quality_metrics,
        }
        if insert_errors:
            self.mark_partial(effective_run_id, error_summary=insert_errors, **common_updates)
            return self._make_result(
                status="PARTIAL",
                run_id=effective_run_id,
                snapshot_date_local=snapshot_date,
                as_of_local=as_of_local,
                as_of_utc=as_of_utc,
                expected_properties=expected_properties,
                attempted_properties=attempted,
                inserted_snapshots=inserted,
                skipped_existing=skipped,
                failed_snapshots=failed,
                error_summary=insert_errors,
            )

        if inserted + skipped != expected_properties:
            summary = ("EXPECTED_SET_NOT_COMPLETE",)
            self.mark_partial(effective_run_id, error_summary=summary, **common_updates)
            return self._make_result(
                status="PARTIAL",
                run_id=effective_run_id,
                snapshot_date_local=snapshot_date,
                as_of_local=as_of_local,
                as_of_utc=as_of_utc,
                expected_properties=expected_properties,
                attempted_properties=attempted,
                inserted_snapshots=inserted,
                skipped_existing=skipped,
                failed_snapshots=0,
                error_summary=summary,
            )

        self.complete_run(effective_run_id, **common_updates)
        return self._make_result(
            status="COMPLETED",
            run_id=effective_run_id,
            snapshot_date_local=snapshot_date,
            as_of_local=as_of_local,
            as_of_utc=as_of_utc,
            expected_properties=expected_properties,
            attempted_properties=attempted,
            inserted_snapshots=inserted,
            skipped_existing=skipped,
            failed_snapshots=0,
        )

    @staticmethod
    def proposed_persistence_contract() -> dict[str, Any]:
        return {
            "collections": [SNAPSHOT_COLLECTION, RUN_COLLECTION],
            "snapshot_id": "v1:{snapshot_date_local}:{normalized_safe_property_code}",
            "snapshot_indexes": [
                {"fields": [("snapshot_date_local", 1)], "unique": False},
                {"fields": list(PROPOSED_IDEMPOTENT_INDEX), "unique": False},
            ],
            "run_indexes": [
                {"fields": [("snapshot_date_local", 1)], "unique": False},
                {"fields": [("status", 1)], "unique": False},
            ],
            # Compatibility label for the Phase 2A contract; actual writes
            # are now enabled only through persist(..., confirm=True).
            "status": "DOCUMENTED_ONLY_NOT_CREATED",
            "status_current": "ENABLED_ONLY_WITH_EXPLICIT_CONFIRMATION",
        }
