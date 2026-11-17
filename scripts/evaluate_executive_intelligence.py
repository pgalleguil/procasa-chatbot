"""Offline synthetic evaluation; --live-model makes explicit paid API calls.

Never reads MongoDB or WaSender. Reports no raw prompts/provider responses.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chatbot.executive_conversation_intelligence import classify_hybrid, _propose_with_deepseek, _verify_with_deepseek


def load_cases(holdout=False):
    folder = ROOT / 'tests/fixtures/executive_intelligence'
    sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location('synthetic_catalog', folder / ('holdout.py' if holdout else 'catalog.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.cases()


def metrics(rows, predicted='predicted'):
    labels = sorted({r['expected'] for r in rows} | {r[predicted] for r in rows})
    confusion = {label: dict(Counter(r[predicted] for r in rows if r['expected'] == label)) for label in labels}
    report = {}
    for label in labels:
        tp = sum(r['expected'] == label and r[predicted] == label for r in rows)
        fp = sum(r['expected'] != label and r[predicted] == label for r in rows)
        fn = sum(r['expected'] == label and r[predicted] != label for r in rows)
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        report[label] = dict(tp=tp, fp=fp, fn=fn, precision=precision, recall=recall,
                             f1=2 * tp / (2 * tp + fp + fn) if tp + fp + fn else None)
    # Unknown/schema-error predictions count as FN for their true category,
    # but are not invented extra business categories in the macro denominator.
    evaluation_labels = sorted({r['expected'] for r in rows})
    f1s = [report[label]['f1'] for label in evaluation_labels]
    return {'per_category': report, 'evaluation_labels': evaluation_labels,
            'macro_f1': sum(f1s) / len(f1s), 'confusion_matrix': confusion}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-model', action='store_true')
    parser.add_argument('--holdout', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--output', default='reports/executive_intelligence/rules.json')
    args = parser.parse_args()
    cases = load_cases(args.holdout)
    if args.live_model:
        # Stratified sample: one context per family, interleave categories.
        families = [c for c in cases if c['variant'] == 0]
        cases = sorted(families, key=lambda c: (int(c['family'].rsplit('-', 1)[1]), c['intent']))
    if args.limit:
        cases = cases[:args.limit]
    rows = []
    for case in cases:
        started = time.monotonic()
        model = {}
        diagnostics = {}
        def propose(messages, prior):
            result = _propose_with_deepseek(messages, prior, diagnostics=diagnostics)
            model.update(result)
            return result
        verifier_calls = []
        verifier_usage = []
        def verify(messages, candidate):
            verifier_calls.append(candidate)
            usage = {}
            verifier_usage.append(usage)
            return _verify_with_deepseek(messages, candidate, diagnostics=usage)
        result = classify_hybrid(case['messages'], llm_enabled=args.live_model, propose=propose,
                                 verify=verify if args.live_model else None)
        rows.append({'id': case['id'], 'family': case['family'], 'expected': case['intent'],
                     'predicted': result['intent'], 'model_predicted': model.get('intent', 'ERROR') if args.live_model else 'NOT_RUN',
                     'needs_review': result['needs_review'], 'source': result['source'],
                     'decision_basis': result['decision_basis'], 'verification': result['verification'],
                     'verifier_calls': len(verifier_calls),
                     'verifier_usage': verifier_usage,
                     'usage': diagnostics,
                     'latency_ms': round((time.monotonic() - started) * 1000)})
        if args.live_model:
            print(f"completed={len(rows)}/{len(cases)} source={result['source']}", flush=True)
    report = {'mode': 'live_deepseek' if args.live_model else 'deterministic_only',
              'cases': len(rows), 'unique_families': len({r['family'] for r in rows}),
              'limitations': ('Synthetic wording holdout; no tuning after observation, not representative prevalence.' if args.holdout else 'Synthetic development set; correlated variants; not held-out, not representative prevalence.') + ' No population 99% claim.',
              **metrics(rows), 'rows': rows}
    if args.live_model:
        report['raw_model_metrics'] = metrics(rows, 'model_predicted')
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('mode', 'cases', 'unique_families', 'macro_f1')}))


if __name__ == '__main__':
    main()
