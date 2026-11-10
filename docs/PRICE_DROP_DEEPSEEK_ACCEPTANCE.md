# Price-drop chatbot: real DeepSeek acceptance run

This report accompanies PR #161. It records isolated end-to-end validation of
the V2 price-drop reply path using synthetic leads and listings. The generator
is DeepSeek; MongoDB is an in-memory `mongomock` database, and customer
delivery, administrative notices, alerts, and CRM links are injected mocks.
The original 40-conversation report is retained as a baseline: its naturalness
heuristic checked only format and length and did not catch several irrelevant
final replies. The focused post-fix run below is the current semantic result.

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

Focused regressions for the final response replacements can be run without
repeating the 40-conversation matrix:

```powershell
python -X utf8 scripts/run_price_drop_deepseek_acceptance.py `
  --env-file C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\.env `
  --scenario-ids synthetic-006 synthetic-025 synthetic-026 synthetic-027 synthetic-036 `
  --calls-limit 40 `
  --output reports/price_drop_deepseek_replacement_regressions.json
```

The focused run uses the same isolated database and fake WaSender/admin sinks.
It is a targeted regression result, not a replacement for the broad scenario
matrix or a certification of live provider behavior.

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

The original saved matrix passed 80/80 checks for property identity, numeric
listing facts, price claims, reply presence/suppression, campaign
classification, continuity, message/queue idempotency, logged response
delivery, and prepared temperature snapshots. Those checks did not measure
whether a final answer addressed an alternatives or offer request. The explicit
visit/contact core-intent guard passed all 80 turns. All 72 model replies were
parsed as structured JSON after enabling
the V2 campaign marker; no unstructured fallback was needed. Mock WaSender
recorded 72 customer deliveries, and mock administrator delivery recorded 24
positive plus 8 informational notices. Executive notices remained frozen.

An earlier pass first misread an acknowledgment that the customer did **not**
want to coordinate a visit. I corrected that evaluator, then regenerated and
rechecked the complete 40-conversation matrix against the final V2 prompt. A
manual scan found the model explicitly avoided claiming lighting effects when
the fixture only supplied orientation. The final report status is `PASS`.

The old naturalness result is a scripted format/length check, not a human
quality rating. A v3 offline replay of that original 80-turn report identified
the five audited defects plus other fact-only alternatives and unsafe executive
contact promises. See
[`reports/price_drop_deepseek_acceptance_recheck_20261110.json`](../reports/price_drop_deepseek_acceptance_recheck_20261110.json)
for the saved-text evaluation; it made no provider or database calls.

## Final-response replacement regressions

After fixing the override path, the five affected synthetic conversations were
rerun with the real DeepSeek generator: **5 conversations, 10 inbound turns,
10 final replies, 16 DeepSeek API requests** (including structured-output
retries). The status is `TARGETED_PASS`.

The corrected final replies retain the natural DeepSeek response when an empty
RAG result verifies a no-match search and the response names the requested
commune. A fact-only answer about the campaign listing is not accepted as a
search reply. Nonempty RAG results still use the deterministic listing renderer.
Offer requests now explain that administration reviews offers with the owner,
and give the chat channel for submitting amount and conditions without
promising that an executive will call or that an offer will be accepted.

All 10/10 final replies passed semantic relevance, alternative target commune,
property identity/facts, current price claims, campaign classification,
context continuity, no-unsolicited-visit, temperature preservation, and
inbound/queue idempotency checks. The run also verified 10/10 customer inbound
messages in `leads.messages`, 10/10 attributed campaign entries in
`price_drop_v2.reply_events`, 10/10 customer-facing outbound message records,
and 10/10 queue delivery events. The fake customer sender recorded 10
deliveries; the fake administrator sender recorded five notices. Executive
notices remained frozen. WaSender credentials were absent and no real
WhatsApp, production MongoDB, deployment, or PR merge occurred.

The paired synthetic-response run is separately labeled
`TARGETED_SIMULATED` and made zero DeepSeek requests. Its report is
[`reports/price_drop_deepseek_replacement_regressions_mock.json`](../reports/price_drop_deepseek_replacement_regressions_mock.json).

The actual post-fix DeepSeek matrix is
[`reports/price_drop_deepseek_replacement_regressions.json`](../reports/price_drop_deepseek_replacement_regressions.json)
and its readable companion is
[`reports/price_drop_deepseek_replacement_regressions.md`](../reports/price_drop_deepseek_replacement_regressions.md).

This remains a synthetic acceptance result. It does not certify live account
behavior, send to the administrator's phone, or authorize sending the three
approved customer messages.

The focused local regression command passed **168 tests**:

```powershell
python -X utf8 -m pytest -q `
  tests/test_price_drop_campaign_cli.py `
  tests/test_price_drop_leads_v2.py `
  tests/test_price_drop_real_pilot.py `
  tests/test_price_drop_customer_response_semantics.py
```

A separate run of `tests/test_chatbot_durable_queue.py` reported six legacy
failures: `create_inbound_job()` does not accept the test's `ingress_timings`
argument; the default batch window ends at +25 seconds while the test expects
+15; three drain/rollback/release assertions expect a `pipeline_version` field
that V1 jobs/batches do not store; and cross-pipeline idempotency creates a new
V1 job for a provider ID previously persisted by V2. The queue implementation,
its V1/V2 runtime helpers, and this test module are byte-for-byte unchanged
from PR base `ee34f209`. The pilot is not deploying or changing runtime modes;
the targeted campaign flow passed its stable-queue delivery and duplicate
checks. The cutover/idempotency expectations remain an operational risk if a
V1/V2 runtime transition is attempted and were not expanded into this fix.

The generated result is stored in
[`reports/price_drop_deepseek_acceptance_latest.json`](../reports/price_drop_deepseek_acceptance_latest.json)
with its readable per-turn companion at
[`reports/price_drop_deepseek_acceptance_latest.md`](../reports/price_drop_deepseek_acceptance_latest.md).
