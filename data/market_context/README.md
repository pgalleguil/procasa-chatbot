# Monthly market context snapshots

The canonical store is `URLS.market_context_snapshots`. Each month is an
immutable period snapshot identified by:

`period + indicator_kind + geography_level + geography_code`

The updater creates/verifies the unique index
`uq_market_context_period_kind_geo`, writes only the requested period, and
never deletes earlier periods. A same-period correction is accepted only from
a complete, verified manifest with the same natural identities; duplicate or
omitted existing identities stop the run.

## First approved period

September 2026 is sourced from the approved `SEPTEMBER_2026_SEED` in
`owner_portal/market_context.py`:

```powershell
python scripts/update_market_context_snapshots.py --period 2026-09
python scripts/update_market_context_snapshots.py --period 2026-09 --execute
```

The first command is read-only. The second requires `MONGO_URI` and is the only
one that creates/verifies the index and writes the requested period.

## Later periods

Add a UTF-8 JSON array of verified documents at
`data/market_context/YYYY-MM.json`, or pass an explicit `--manifest` path.
Every document must include the canonical metadata fields and match the
requested period. Do not copy the prior month's values into a new period.

```powershell
python scripts/update_market_context_snapshots.py --period 2026-10
python scripts/update_market_context_snapshots.py --period 2026-10 --execute
```

The default invocation is a dry-run. If no verified manifest exists, the
updater fails closed and performs no database writes.
