# Price-drop chatbot: real DeepSeek acceptance run

This report accompanies PR #161. It records an isolated end-to-end validation
of the V2 price-drop reply path using synthetic leads and listings. The actual
conversation generator is DeepSeek; MongoDB is an in-memory `mongomock`
database, and customer delivery, administrative notices, alerts, and CRM links
are injected mocks.

## Run

From the repository root on Windows, using the private `.env` already used for
the generator:

```powershell
python -X utf8 scripts/run_price_drop_deepseek_acceptance.py `
  --env-file C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\.env `
  --conversations 40 --calls-limit 220 `
  --output reports/price_drop_deepseek_acceptance_latest.json
```

Only `DEEPSEEK_API_KEY` (or the legacy `XAI_API_KEY` key name) and
`DEEPSEEK_BASE_URL` are read from the private environment file. The endpoint is
restricted to `https://api.deepseek.com`. Do not paste, print, or commit that
file. One conversation set contains two inbound messages per lead; global
opt-outs and completed-search messages are intentionally suppressed by the
reply path. The report writes JSON and a same-name Markdown matrix next to the
JSON file. The real-provider run requires at least 40 conversations and has a
hard request cap.

`--mock-deepseek --conversations 10` is only a local plumbing smoke test. Its
status is `SIMULATED` and does not count as generator acceptance.

## What the matrix checks

- Actual `record_inbound()`, active V2 campaign context, durable inbound queue,
  real chatbot core, classifier, event log, and mock-delivery reconciliation.
- UF and CLP current prices, property identity, successive drops, attribution
  to the latest event, and HOT/NORMAL/COLD campaign snapshots.
- Opt-out and search-completed suppression; explicit contact, no-call, offer,
  alternatives, mixed visit refusal, and affirmative visit intent.
- Context continuity, response presence, a format/length naturalness heuristic,
  and deterministic duplicate handling for inbound IDs and queue jobs.
- Administrative positive and informational notices using an allowlisted fake
  sender; notice content, one-shot behavior, and frozen executive notices.

Naturalness is a scripted heuristic, not a human language-quality rating.
Property/price claims and operational intent checks are evaluated against the
synthetic fixtures and recorded events. The report retains every input and
customer-visible generated answer to support manual review.

## Safety boundaries

- Synthetic phone-shaped IDs and synthetic message text are used; phone fields
  are removed from prompts. No customer conversation content is sent to
  DeepSeek.
- The process removes inherited MongoDB, WaSender, and campaign-send settings
  before importing application modules. It refuses startup if production config
  or WaSender credentials remain visible.
- All campaign state is held in a new in-memory database. WaSender, admin
  delivery, alerting, and CRM links are mocked. No WhatsApp, production Mongo,
  approved production campaign, deployment, or PR merge is involved.
- Provider call count is capped, but that cap is a test guard and does not
  guarantee acceptance by or avoid restrictions from the provider.

## Results

The final run, after tightening the V2 prompt to avoid unsupported property
inferences, used the real DeepSeek generator with **40 synthetic conversations
/ 80 inbound turns**. It produced **72 structured model replies**; the eight
explicit global opt-outs or completed searches were correctly suppressed. The
generator made **95 API requests**, including structured-format retries. All
40 campaign contexts were verified; UF and CLP properties, successive
reductions, and HOT/NORMAL/COLD lead snapshots were covered.

The saved matrix passes 80/80 checks for property identity, numeric listing
facts, price claims, reply presence/suppression, campaign classification,
continuity, message/queue idempotency, logged response delivery, and prepared
temperature snapshots. The explicit-visit/contact core-intent guard passed all
80 turns. All 72 model replies were parsed as structured JSON after enabling
the V2 campaign marker; no unstructured fallback was needed. Mock WaSender
recorded 72 customer deliveries, and mock administrator delivery recorded 24
positive plus 8 informational notices. Executive notices remained frozen.

An earlier pass first misread an acknowledgment that the customer did **not**
want to coordinate a visit. I corrected that evaluator, then regenerated and
rechecked the complete 40-conversation matrix against the final V2 prompt. A
manual scan found the model explicitly avoided claiming lighting effects when
the fixture only supplied orientation. The final report status is `PASS`.

The naturalness result is a scripted format/length check, not a human quality
rating. This synthetic acceptance result does not certify the chatbot as ready
for live operation. Production WhatsApp delivery, provider behavior under a
live account, and human review of language quality remain outside this run.

The focused local regression command passed **156 tests**:

```powershell
python -X utf8 -m pytest -q `
  tests/test_price_drop_campaign_cli.py `
  tests/test_price_drop_leads_v2.py `
  tests/test_price_drop_real_pilot.py
```

A separate local run also included `tests/test_chatbot_durable_queue.py` and
reported six failures in legacy queue expectations (`ingress_timings`, batch
window extension, and V1/V2 `pipeline_version` routing/idempotency). Neither
that queue implementation nor its legacy test file changed in this work. The
campaign-focused flow itself passed the 80-turn durable queue and delivery-log
checks above; GitHub CI for the updated PR is reported separately.

The generated result is stored in
[`reports/price_drop_deepseek_acceptance_latest.json`](../reports/price_drop_deepseek_acceptance_latest.json)
with its readable per-turn companion at
[`reports/price_drop_deepseek_acceptance_latest.md`](../reports/price_drop_deepseek_acceptance_latest.md).
