# PROCASA SUCRE publication synchronization

This changeset adds an isolated publication synchronizer for Prop360 office ID
7. It does not enable a scheduled service, deploy Render, or write production
Mongo data by itself.

## Root cause and live lead path

`Prop360Client.get_portales()` supplies current publication records, but the
former `upsert_ficha()` publication merge replaced provider subdocuments
shallowly. That could discard older URLs and nested publication data. The
source response also contains the Proppit aggregator URL rather than its five
syndicated destinations.

For an inbound WhatsApp reference, `chatbot/processing_service.py` calls
`resolver_referencia_propiedad(..., use_legacy_lookup=False)`. That resolver
uses the shared `lookup_property_link()` in `chatbot/property_lookup.py`. The
previous central lookup did not recognize Proppit or its five destination
domains, so a URL delivered by those portals had no exact, persisted identity
to match. The implementation keeps that production call path and uses
`publicaciones.aliases` as the one current/historical identifier index.

## Data model

The stable identity remains `codigo` from Prop360. Existing compatibility
fields stay under `publicaciones.portal_inmobiliario`, `procasa`, `toctoc`,
`yapo`, `proppit`, and `chilepropiedades`. `publicaciones.aliases[]` stores each
verified current or historical provider/operation reference. A historical
entry remains resolvable even when `activa=false`; only an explicit
`resolvable=false` or an unverified/conflicted record blocks a match.

Observed syndicated destinations are stored under:

```text
publicaciones.proppit.portales_distribucion.{provider}.{V|A}
```

Each destination records its current URL, external ID, observed URL list,
source URL, verification state, destination-check state, and the timestamp of
the latest changed observation. The Proppit aggregator URL remains in the
existing `url_proppit` field and has its own alias. Missing provider links are
not synthesized from a shared UUID.

On a changed URL/ID, the previous current reference is copied to
`publicaciones.aliases` as historical and resolvable, and the legacy URL arrays
accumulate known URLs. Partial source responses do not erase existing values.
An explicit verified absence from a Proppit aggregator can retire a syndicated
URL from `url_actual`; the old URL stays in `urls_historicas` and aliases.

## 16479 source verification and current limitation

The issue records the stored old reference:

| Reference | URL / value | State before a fresh source check |
| --- | --- | --- |
| Old Mercado Libre | `MLC-3842833828`, `http://casa.mercadolibre.cl/MLC-3842833828-casa-en-venta-de-8-dorm-en-lo-prado-_JM` | Observed in Mongo; historical after a verified replacement |
| New Mercado Libre | `MLC-4548780920`, `https://casa.mercadolibre.cl/MLC-4548780920-casa-en-venta-de-8-dorm-en-lo-prado-_JM` | Confirmed as the current `V` publication by authenticated `Prop360Client.get_portales()` on 2026-10-16 |
| Proppit aggregator | `https://links.proppit.com/links?crm=real_time_convecta&country=cl&id=10543041010016479&agency=ProcasaSucre-1-543-7` | Redirects to `proppit.com/property-links/...`; current `robots.txt` denies this crawler, so the HTML was not fetched |
| iCasas, Trovit, Mitula, Nuroa, Nestoria | URLs in the issue's fixture | Test fixtures only; never inserted or reported as currently observed without seeing them in the aggregator response |

The authenticated read-only source check found the old MLC in current Mongo
fields and no existing alias for it. Prop360 returned `MLC-4548780920` as the
current Venta publication; the dry-run proposes the old MLC as historical and
resolvable while making the new MLC current. No Mongo update was made. The
Proppit-derived URLs remain unverified because the source's robots policy
forbids fetching the redirected page. The implementation intentionally does
not bypass that policy or synthesize destination URLs.

The authenticated Prop360 listing contained 441 active unique Sucre codes;
Mongo contained 442 active unique Sucre documents. Code `6579` was active in
Mongo but absent from Prop360's complete active listing (1,178 listing rows,
three complete pages). A separate authenticated read-only status query on
2026-10-16 found `6579` in Prop360 with listing state `Pasiva`, captador Susana
Ensignia, and address sector Los Leones / Ñuñoa; Mongo still has
`disponible_prop360=true` and `estado_prop360=Activa`. Its embedded
`historial_cambios` contains only `metadata.version`; the dedicated
`universo_cartera_prop360_historial` has no `6579` row. The saved portfolio
sync status is from 2026-09-30, reports `apply_bajas=false` and
`bajas_aplicadas=0`, and also contains `error=Prop360AuthError` despite a
`completed` status. It does not prove a later state transition. This remains a
Prop360/Mongo status mismatch with no recorded transition; no document was
changed or automatically declared baja.

The initial full per-property publication dry-run was not completed: the first
detail request exceeded the previous retry/timeout window. A local resumable
auditor is now available. It snapshots the validated active listing and only
publication/office identity fields, processes at most 25 codes per invocation,
stores each property attempt incrementally, and retries recorded timeouts on
resume. A run reports `coverage_complete` separately from link verification;
`timeout`, `blocked`, `conflict`, and `unverified` are never counted as a
verified URL or a successful update. The original 441-code snapshot must all
have traceable provider rows before coverage is complete. Every audit batch is
read-only against Mongo.

## Sync behavior and safety

- Full reconciliation reads the complete, pagination-validated Prop360 active
  listing for office 7, then reads only the matching Mongo documents and
  publication fields needed for identity claims. It does not download images,
  embeddings, owner PII, or price data.
- Mongo scope requires an active property and consistent Sucre office identity.
  Missing, inactive, contradictory, or duplicate Mongo codes are quarantined.
- Identity collisions are checked against the whole collection. Matching MLCs
  share one portal namespace, and collisions are blocked even if records say
  different operations. Conflicting properties receive no publication write.
- A write uses `$set: {publicaciones: ...}` only, with a compare-and-swap filter
  over `_id`, `codigo`, active state, all present office identifiers, and the
  previous publications snapshot. Other offices and non-publication fields are
  outside the write path.
- Before each write batch, the affected whole documents are written to a
  compressed BSON-JSON backup and read back/count-checked. The batch worker
  requires a configured private backup directory for `--execute`.
- New active property ingestion persists publication data only when
  `PUBLICATION_SYNC_EXECUTE=true`,
  `PUBLICATION_SYNC_NEW_PROPERTIES_ENABLED=true`, and a verified backup
  destination are all configured. Otherwise it does not write publications.
  Existing-property writes also require the execute flag, explicit `--execute`,
  office 7, a bound of at most 25 properties, and backup readback verification.
- The incremental worker detects publication-only changes independent of
  price, executive, and listing fingerprint; it is not imported by webhook or
  application startup.
- Proppit provider omissions and transient failures stay pending. The retry
  policy is 1h, 6h, then every 24h. Robots denials and other permanent security
  blocks are recorded as `blocked` without quick retries; the normal full
  portfolio cycle may recheck the policy later. A pending code is excluded
  from sequential scanning until its retry is due. All active codes are
  reconciled in batches of at most 25 using a persisted cursor.
- No scraping occurs on the synchronous WhatsApp path.

## Commands

Start the resumable, read-only active Sucre audit (maximum 25 properties per
invocation):

```powershell
python scripts/audit_sucre_publications.py --batch-size 25 --output-dir "$env:LOCALAPPDATA\Procasa\publication-audit"
```

Use the returned `run_dir` to continue the same initial snapshot. Successful
and terminally blocked/conflicted properties are skipped; timeouts are retried,
and each attempt remains in `events.jsonl`:

```powershell
python scripts/audit_sucre_publications.py --resume "$env:LOCALAPPDATA\Procasa\publication-audit\sucre_publication_audit_<run_id>" --batch-size 25
```

The run directory contains `manifest.json`, `checkpoint.json`, append-only
`events.jsonl`, consolidated `results.json` / `results.csv`, and, once every
initial code has a traceable result, `final_listing.json` plus inventory changes
in `summary.json`. The per-property HTTP budget defaults to 35 seconds; the
audit Prop360 session uses one request attempt with an 8-second request timeout,
and its portal JSON is consumed as a stream under the same per-property
deadline.
Proppit fetches use the remaining property budget and one bounded request
attempt while honoring robots rules. Change the budget with
`--property-budget-seconds` (5–120 seconds). A timeout stays unverified and is
never represented as a successful URL observation. DNS A/AAAA validation,
robots retrieval, request timeouts, and streamed response bodies all share the
per-property deadline; an early timeout is recorded and does not block later
codes in the initial pass.

Run the full active Sucre dry-run. This writes local JSON/CSV report files but
does not write Mongo, acquire a Mongo lock, or update synchronization state:

```powershell
python scripts/reconcile_sucre_publications.py --office-id 7 --output-dir "$env:LOCALAPPDATA\Procasa\publication-sync\reports"
```

Run a read-only 16479 source check before any canary:

```powershell
python scripts/reconcile_sucre_publications.py --office-id 7 --codigo 16479 --output-dir "$env:LOCALAPPDATA\Procasa\publication-sync\reports"
```

Only after reviewing the read-only result and authorizing a canary, use a
durable private backup directory. Existing-property writes require both the
command flag and an explicit environment enable flag. On Render, also set
`PUBLICATION_SYNC_BACKUP_DURABLE_ROOT` to the persistent disk mount point; the
runner verifies the backup directory is below that mounted root and performs a
write/readback probe before any Mongo lock or sync-state write. A one-property
write is explicitly bounded:

```powershell
$env:PUBLICATION_SYNC_EXECUTE = "true"
python scripts/reconcile_sucre_publications.py --office-id 7 --codigo 16479 --limit 1 --execute --backup-dir "$env:LOCALAPPDATA\Procasa\publication-sync\backups"
```

The full portfolio command remains dry-run by default. Do not remove the
`--office-id 7` constraint or run it with an unbounded `--execute`.

One periodic worker invocation, dry-run by default:

```powershell
python scripts/run_sucre_publication_sync_batch.py --office-id 7 --limit 25
```

The execute form requires a private, durable backup directory:

```powershell
python scripts/run_sucre_publication_sync_batch.py --office-id 7 --limit 25 --execute --backup-dir "$env:LOCALAPPDATA\Procasa\publication-sync\backups"
```

The batch CLI forwards `--backup-dir` and `--output-dir`. Dry-run never updates
the Mongo cursor. Execute results use `completed`, `partial`, `failed`, or
`no_due_work`. Per-property failures are retained with a specific error,
attempt count, and `next_retry_at`; the cursor advances only when each selected
code has a terminal result or a persisted retry-queue entry.

New-property publication sync is disabled unless both flags and a verified
backup directory are configured:

```text
PUBLICATION_SYNC_EXECUTE=true
PUBLICATION_SYNC_NEW_PROPERTIES_ENABLED=true
PUBLICATION_SYNC_BACKUP_DIR=<durable-private-backup-path>
```

Historical backfill remains manual and bounded: use `reconcile_sucre_publications.py`
with explicit `--office-id 7`, explicit `--codigo` values, `--limit` from 1 to
25, `--execute`, the enable flag, and a verified backup path. Do not schedule
historical backfill.

## Enabling the scheduled reconciliation later

`chatbot.publication_sync_worker` is an opt-in, separate process and is not
imported by `webhook.py` or application startup. It defaults to inactive and
dry-run. Do not enable its write flag until the full audit and controlled
canary pass. Render Cron Jobs cannot mount persistent disks, so they cannot
meet this worker's required durable backup path; use a separate Render
Background Worker with a private persistent disk instead ([Render Cron docs](https://render.com/docs/cronjobs),
[Render disk docs](https://render.com/docs/disks)).

After the full dry-run is complete and the canary is approved, create one
Render Background Worker from the deployed repository, using the chatbot's
runtime/build dependencies. Attach a private disk mounted at `/var/data` and
set the command:

```text
python -m chatbot.publication_sync_worker --interval-seconds 1200
```

Set these environment values in the worker only:

```text
PUBLICATION_SYNC_ENABLED=true
PUBLICATION_SYNC_EXECUTE=false
PUBLICATION_SYNC_INTERVAL_SECONDS=1200
PUBLICATION_SYNC_BACKUP_DIR=/var/data/publication-sync-backups
PUBLICATION_SYNC_BACKUP_DURABLE_ROOT=/var/data
PUBLICATION_SYNC_REPORT_DIR=/var/data/publication-sync-reports
```

Start with `PUBLICATION_SYNC_EXECUTE=false`; review dry-run reports and confirm
the worker's batch duration and Prop360 response latency. The live read-only
check in this task took over two minutes for one publication request, so a
20-minute interval is not yet proven to complete 25-property batches or a
six-hour full cycle. After that is measured and the controlled canary passes,
set `PUBLICATION_SYNC_EXECUTE=true`. Both the worker enable flag and execute
flag are required. The backup root must be the mounted private disk and is
verified before any Mongo write. To enable new-property publication sync as
well, set `PUBLICATION_SYNC_NEW_PROPERTIES_ENABLED=true`. The backup and
report paths must remain on the private disk. The worker waits the configured interval after a batch finishes, so it
does not overlap its own work. Do not attach this loop to webhook startup or
enable `ficha_sync_loop` globally.

No scheduled worker is enabled or deployed by this PR.

## Authorized Proppit/Convecta destination-feed contract

HTML scraping remains subject to provider robots policy. An authorized API,
feed, platform export, or evidence-backed administrative import can use
`chatbot.publication_import.normalize_authorized_publication_rows()` and the
`normalize_authorized_publication_feed.py` CLI. All sources map to the existing
`publicaciones.aliases[]` and
`publicaciones.proppit.portales_distribucion.{provider}.{V|A|R}` data contract.
A declared URL is stored as unverified and `resolvable=false`; only explicit
attribution confirmation with an evidence reference and source record ID makes
it resolvable. Availability is separate and is never inferred from a URL or
message text.

Request Proppit/Convecta to provide a documented API or export with one JSON
record per syndicated destination and these fields:

```json
{
  "prop360_code": "16479",
  "agency_id": "ProcasaSucre-1-543-7",
  "aggregator_publication_id": "10543041010016479",
  "publication_id": "provider-specific-id",
  "provider": "icasas|trovit|mitula|nuroa|nestoria",
  "operation": "V|A|R",
  "destination_url": "https://provider.example/actual-listing",
  "destination_external_id": "provider-specific-id",
  "publication_status": "active|historical|removed",
  "attribution_status": "confirmed|declared|refuted",
  "availability_status": "available|unavailable|unknown",
  "verified_at": "ISO-8601 timestamp or null",
  "source_record_id": "stable-id-in-provider-feed",
  "evidence_reference": "provider API record, export row, or signed audit reference",
  "updated_at": "ISO-8601 timestamp"
}
```

The contract must state whether the API is authoritative for publication
ownership; whether operations are emitted independently; how removals and
republications appear; the uniqueness scope and stability of IDs; pagination
and incremental-cursor semantics; authentication and rate limits; and provide
a signed or otherwise verifiable sample for the five 16479 destinations. The
five user-supplied links remain test fixtures only until source evidence
confirms code, provider, operation, and publication state.

## Restore procedure

First inspect the target backup without writes:

```powershell
python scripts/restore_sucre_publications_backup.py "<backup-path>" --office-id 7
```

After confirming all backup documents remain active Sucre records, restore
only `publicaciones`. The script makes and verifies a safety backup of the
current documents before restoring:

```powershell
python scripts/restore_sucre_publications_backup.py "<backup-path>" --office-id 7 --execute --safety-backup-dir "<private-safety-backup-dir>"
```

It aborts if any target is missing, inactive, outside Sucre, contradictory,
or if a compare-and-swap write fails. It never restores or edits prices, CRM,
ownership, images, embeddings, leads, or other document fields.

## Local verification

The local fixtures in `tests/test_prop360_publication_issue46.py` cover the
16479 MLC transition, Proppit aggregator and five destinations, current and
historical resolution through the production link resolver, the
`LeadProcessingService.classify` route, conflict refusal, active/inactive
office scope, source parsing, retry scheduling, no-write dry-run, and
idempotent alias/history merging. Run with:

```powershell
python -m pytest -q tests/test_prop360_publication_issue46.py tests/test_property_alias_resolution.py
```

The fixtures do not prove that the new 16479 MLC or syndicated URLs are live;
the authenticated read-only dry-run is the evidence source for that.
