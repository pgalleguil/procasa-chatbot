# Acceptance report: price-drop campaign classifier

**Scope:** PR #161, classifier and reply-attribution corrections. This is an
isolated regression/acceptance run, not a production-readiness certification.

## Results

- 66 phrase-level regression cases cover opt-outs, spelling errors, call and
  visit preferences, mixed requests, completed searches, offers, alternatives,
  commune references, and negative examples.
- 40 synthetic conversations passed, with 4 turns each (160 turn records).
- 20 conversations introduced a second price-drop event after the first reply;
  subsequent replies were attributed to the newer event.
- Temperature coverage: HOT 14, NORMAL 13, COLD 13.
- Property coverage includes synthetic UF and CLP listings and verified
  successive price changes.
- The final JSON matrix contains every input, generated mock response,
  expected/obtained classification, event ID, checks, outcome, and evidence.
- Duplicate inbound provider IDs did not create another V2 reply event.
- Opt-outs produced no chatbot/customer response and set global suppression.
- Positive notices were queued in lead state; administrative notice behavior
  and one-shot delivery are separately covered with an injected sender mock.
- Provider receipts in the simulation mean **accepted by the mock only**;
  no delivery or read status is inferred.

The focused classifier and 40-conversation regression run completed with
**73 passed**. The broader related local run completed with **164 passed and 6 deselected**.
The six deselected tests in `test_chatbot_durable_queue.py` expect unrelated
queue fields/signatures that are absent from the PR base (`ingress_timings`,
`pipeline_version`, and a non-sliding batch deadline). The separate legacy
`test_chatbot_phase1.py` module cannot be collected on this base because it
imports three missing visit-helper functions. None of those expectations are
changed by the classifier or attribution edits in this update.

## Isolation and limits

All Mongo access was routed to `mongomock`; the test WaSender sender only
records calls and returns synthetic provider IDs. No production MongoDB,
WhatsApp endpoint, deployment, or approved campaign was touched.

The production conversation model was not invoked. The matrix injects a
deterministic Spanish response callback into the actual durable queue, with
actual `record_inbound()`, `active_context()`, conversation policy checks,
message logging, and queue delivery-state reconciliation. The local model
configuration did not provide a usable real generator for an isolated run.
Naturalness is therefore checked by a scripted language/relevance rubric;
this result does not replace review of live-model output.

The classifier, opt-out persistence, queue suppression, and notice regression
cases pass. I am not marking the chatbot ready for live operation based on
these mocked generation results.

## Reproduction

```powershell
python -m pytest tests/test_price_drop_campaign_classifier_regression.py tests/test_price_drop_campaign_acceptance_simulation.py -q
python scripts/run_price_drop_classifier_acceptance.py
```

The generated turn-by-turn matrix is
[`reports/price_drop_classifier_acceptance_2026-11-09.json`](../reports/price_drop_classifier_acceptance_2026-11-09.json).
