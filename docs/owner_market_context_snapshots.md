# Owner Portal market context snapshots

`URLS.market_context_snapshots` is the shared, period-frozen source for macro
and housing-market context. It stores one verified indicator per document; it
does not copy the same indicators into every `owner_property_portals` record.

Natural key:

```text
period + indicator_kind + geography_level + geography_code
```

Each document uses these fields:

```yaml
period: YYYY-MM
indicator_kind: MORTGAGE_REFERENCE | TPM | UNEMPLOYMENT | REAL_WAGES | CPI | REGIONAL_HOME_SALES | REGIONAL_RENTAL_INDICATOR | FOGAES_CONTEXT
value: number
display_value: string
unit: string
observation_period: string
geography_level: COUNTRY | REGION | MARKET
geography_code: string
geography_label: string
source_name: string
source_reference: string
source_published_at: date | null
verified_at: date
relevant_operations: [VENTA, ARRIENDO]
methodology_notes: string
verified: true
active: true
```

The portal reads only documents matching the report's exact `YYYY-MM` period.
It then filters by the report's resolved operation and property's geography.
Regional unemployment may fall back to a verified national series, which is
displayed with the `Chile` label. Market-level Gran Santiago data is only
eligible for properties in Región Metropolitana. There are no live source
requests during portal rendering. Missing or ambiguous data is omitted.

The September 2026 seed includes the June-August 2026 INE unemployment rate
for all 16 Chilean regions, plus a national fallback. Regional rental
indicators are not seeded; the portal will only promote one from the canonical
collection when it has verified source, period, and explicit regional scope.
QA or legacy monthly rental indicators are excluded from production rendering.

`scripts/seed_market_context_snapshots.py` is read-only by default. The
`--execute` flag is required to write; it prints inserts, updates, unchanged
records, and natural-key conflicts before applying the seed.
