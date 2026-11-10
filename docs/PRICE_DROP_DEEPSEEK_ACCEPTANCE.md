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

The generated result is stored in
[`reports/price_drop_deepseek_acceptance_latest.json`](../reports/price_drop_deepseek_acceptance_latest.json)
with its readable per-turn companion at
[`reports/price_drop_deepseek_acceptance_latest.md`](../reports/price_drop_deepseek_acceptance_latest.md).
This section should be updated with the completed run status, pass counts, any
remaining critical defects, and the CI results before the PR is updated.
