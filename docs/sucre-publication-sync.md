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
three complete pages). It is reported as an inventory mismatch and is not
treated as a property baja or synchronized. A complete per-property publication
dry-run was not completed: Prop360's first publication-detail request exceeded
the local read window. The single-property 16479 dry-run completed with zero
Mongo writes; the full run must be repeated when Prop360 responds reliably.

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
- New active Sucre property ingestion calls the same publication reconciler
  immediately after the ficha is stored. Existing properties are covered by
  the incremental worker, which detects publication-only changes independent
  of price, executive, and listing fingerprint.
- Proppit provider omissions and transient failures stay pending. The retry
  policy is 1h, 6h, then every 24h. Robots denials and other permanent security
  blocks are recorded as `blocked` without quick retries; the normal full
  portfolio cycle may recheck the policy later. A pending code is excluded
  from sequential scanning until its retry is due. All active codes are
  reconciled in batches of at most 25 using a persisted cursor.
- No scraping occurs on the synchronous WhatsApp path.

## Commands

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
private backup directory. A one-property write is explicitly bounded:

```powershell
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
PUBLICATION_SYNC_REPORT_DIR=/var/data/publication-sync-reports
```

Start with `PUBLICATION_SYNC_EXECUTE=false`; review dry-run reports and confirm
the worker's batch duration and Prop360 response latency. The live read-only
check in this task took over two minutes for one publication request, so a
20-minute interval is not yet proven to complete 25-property batches or a
six-hour full cycle. After that is measured and the controlled canary passes,
set `PUBLICATION_SYNC_EXECUTE=true`. Both the explicit enable flag and execute
flag are required; the backup and report paths must remain on the private
disk. The worker waits the configured interval after a batch finishes, so it
does not overlap its own work. Do not attach this loop to webhook startup or
enable `ficha_sync_loop` globally.

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
