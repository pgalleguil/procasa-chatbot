# TOCTOC pipeline operations

## Canonical scraper entrypoint

For a discovery-and-processing run, use `scrapers/scraper_toctoc/run_toctoc.py`
with its `run-full` command. It creates a batch ID, persists the discovery
checkpoint, and passes the resulting listing set through the existing detail
fetch, extraction, classification, persistence, and optional distribution
stages. `discover` and `process` remain available for explicit resumable
operation using the same batch ID. Do not run a legacy classifier or distributor
as a parallel path.

The reusable Python adapter is
`scrapers.scraper_toctoc.pipeline_entrypoint.run_configured_toctoc_pipeline`;
it accepts a `ToctocRunConfig` and a candidate-set file, then delegates detail
processing to the same production runner. The core
`toctoc_pipeline.run_toctoc_pipeline` is dependency-injected and is also used
for controlled/replay execution. A multi-commune scope must be assembled by the
caller before invoking the candidate-set adapter; do not interpret a single
`run-full` query as a multi-commune run.

## AI classification and retry policy

All productive classification calls pass through `classification_service`.
Output and attempt policy is centralized in the TOCTOC config:

- `AI_OUTPUT_TOKEN_BUDGET_DEFAULT` (legacy alias: `DEEPSEEK_MAX_TOKENS`)
- `AI_OUTPUT_TOKEN_BUDGET_RETRY` (bounded technical retry ceiling)
- `AI_MAX_ATTEMPTS_PER_FINGERPRINT` (clamped to at most two total attempts)
- `MAX_AI_CALLS_PER_RUN`, `MAX_INPUT_TOKENS_PER_RUN`,
  `MAX_OUTPUT_TOKENS_PER_RUN`, and `MAX_ESTIMATED_COST_PER_RUN`

Only `finish_reason=length` permits one technical retry for an unchanged
classification fingerprint. The retry uses the configured higher ceiling; a
second truncation remains `AI_FAILED_RETRYABLE` and is never treated as a
completed business classification. Each HTTP attempt is recorded in
`deepseek_call_ledger` before the request, then updated with the response,
provider usage, parser status, and raw content/reasoning. Credentials are never
stored in the ledger.

To retry eligible truncated attempts, invoke
`scrapers/scraper_toctoc/retry_failed_deepseek.py` with the source run ID and a
bounded maximum item count. If `--details-json` is omitted, it loads only the
selected existing property documents from Mongo by `listing_id`; it does not
scrape, write property documents, or assign listings. The ledger and exact
fingerprint determine eligibility, so successful attempts and exhausted
fingerprints are not called again.

## Resume and safety

Discovery checkpoints are stored as `discovered_<batch_id>.json`; processing
reports are stored as `processed_<batch_id>.json`. Reuse the existing batch ID
to resume a processing batch rather than generating a new ID for the same
checkpoint. Before enabling a production run, confirm Mongo, extraction health,
proxy configuration, AI budgets, and assignment invariants. Unknown or
truncated AI results remain non-assignable.
