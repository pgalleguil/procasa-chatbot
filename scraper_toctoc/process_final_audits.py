#!/usr/bin/env python
"""Final audits: INCIERTO classification + commune backfill. Apply if coherent."""
import json, sys, os, re
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\scraper_toctoc')
from mongo_store import MongoStore
from config import AppConfig
from classifier_rules import (
    detect_explicit_owner, detect_weak_owner, detect_professional_project_signals,
    classify_structural_broker, classify_structural_owner, classify_obvious_broker,
    build_rule_context, NEUTRAL_CONTACT_PHRASES
)

col = MongoStore(AppConfig()).collection()
OUT = Path(r'reports')
INCIERTO_DIR = OUT / 'final_uncertain_audit'
COMMUNE_DIR = OUT / 'commune_backfill'
INCIERTO_DIR.mkdir(parents=True, exist_ok=True)
COMMUNE_DIR.mkdir(parents=True, exist_ok=True)

# ===========================
# PART 1: INCIERTO AUDIT
# ===========================
print("=" * 80)
print("PART 1: AUDITING 1,511 INCIERTO")
print("=" * 80)

incierto_cursor = col.find({'origen': 'toctoc', 'classification.state': 'INCIERTO'},
                           {'_id': 0, 'listing_id': 1, 'url': 1, 'title': 1, 'description': 1, 'descripcion': 1,
                            'classification': 1, 'seller_type': 1, 'seller_name': 1, 'html_path': 1})
stats = {"total": 0, "to_ds": 0, "to_cs": 0, "to_cp": 0, "stay_incierto": 0}
changes = []

for doc in incierto_cursor:
    lid = doc['listing_id']
    desc = doc.get('description', '') or ''
    title = doc.get('title', '') or ''
    seller_name = doc.get('seller_name', '') or ''
    text = f"{desc} {title} {seller_name}"
    
    extracted = {
        "seller_type": doc.get('seller_type', 'DESCONOCIDO'),
        "seller_type_source": "",
        "seller_type_evidence": "",
        "url_format": "",
        "description": desc,
        "descripcion": desc,
        "title": title,
        "seller_text": "",
        "seller_name": seller_name,
        "publicador_visible": seller_name,
        "contact_name": "",
        "listing_advertiser": "",
        "html_validation_status": "OK",
    }
    
    brok = classify_structural_broker(extracted)
    if brok:
        proposed = brok['state']
        reason = brok.get('reason', '')
        signals = [s['signal'] for s in detect_professional_project_signals(text)]
        literal = signals
    else:
        owner = classify_structural_owner(extracted)
        if owner:
            proposed = owner['state']
            reason = owner.get('reason', '')
            literal = [e for e in owner.get('evidence', []) if 'seller_type' not in e]
        else:
            obv = classify_obvious_broker(extracted)
            if obv:
                proposed = obv['state']
                reason = obv.get('reason', '')
                literal = obv.get('evidence', [])
            else:
                proposed = 'INCIERTO'
                reason = 'Sin evidencia suficiente'
                literal = []
    
    stats["total"] += 1
    if proposed == 'DUEÑO_SEGURO': stats["to_ds"] += 1
    elif proposed == 'CORREDOR_SEGURO': stats["to_cs"] += 1
    elif proposed == 'CORREDOR_PROBABLE': stats["to_cp"] += 1
    else: stats["stay_incierto"] += 1
    
    if proposed != 'INCIERTO':
        changes.append({"listing_id": lid, "current": "INCIERTO", "proposed": proposed, "reason": reason[:100], "literal": literal[:3]})

print(f"\nINCIERTO audit results:")
for k, v in stats.items():
    print(f"  {k}: {v}")

# Save
with open(INCIERTO_DIR / 'report.json', 'w') as f:
    json.dump(stats, f, ensure_ascii=False, indent=2)
with open(INCIERTO_DIR / 'proposed_changes.jsonl', 'w') as f:
    for c in changes:
        f.write(json.dumps(c, ensure_ascii=False) + '\n')
with open(INCIERTO_DIR / 'report.md', 'w') as f:
    f.write(f"# INCIERTO Audit\n\nTotal: {stats['total']}\n")
    for k, v in stats.items():
        f.write(f"- {k}: {v}\n")
    for c in changes:
        f.write(f"- {c['listing_id']}: {c['current']} -> {c['proposed']} | {c['reason'][:60]}\n")

# ===========================
# PART 2: COMMUNE BACKFILL
# ===========================
print(f"\n{'='*80}")
print("PART 2: COMMUNE BACKFILL")
print("=" * 80)

COMMUNE_ACCENTS = str.maketrans('áéíóúñüÁÉÍÓÚÑÜ', 'aeiounuAEIOUNU')

def _norm_comuna(c):
    c = c.strip().lower().replace('-', ' ').replace('_', ' ')
    c = c.translate(COMMUNE_ACCENTS)
    c = re.sub(r'\s+', ' ', c).strip()
    return c

def extract_comuna_from_url(url):
    if not url:
        return ''
    # Pattern: /propiedades/<format>/<tipo>/<comuna>/...
    m = re.search(r'/propiedades/[^/]+/[^/]+/([^/]+)/', url)
    if m:
        return m.group(1)
    # Pattern: /propiedad/<slug>-<id>
    m = re.search(r'/propiedad/([^-]+)', url)
    if m:
        return m.group(1).replace('-', ' ')
    return ''

comuna_stats = {"total": 0, "already_have": 0, "empty": 0, "filled": 0, "conflicts": 0, "no_source": 0}
commune_updates = []
commune_conflicts = []

for d in col.find({'origen': 'toctoc'}, {'_id': 0, 'listing_id': 1, 'url': 1, 'comuna': 1, 'html_path': 1}):
    lid = d['listing_id']
    existing = d.get('comuna', '') or ''
    url = d.get('url', '') or ''
    
    comuna_stats["total"] += 1
    if existing and existing.strip():
        comuna_stats["already_have"] += 1
        continue
    
    # Extract from URL
    url_comuna = extract_comuna_from_url(url)
    if url_comuna:
        comuna_stats["filled"] += 1
        commune_updates.append({"listing_id": lid, "comuna": url_comuna, "comuna_slug": _norm_comuna(url_comuna), "source": "url"})
    else:
        comuna_stats["empty"] += 1

print(f"\nCommune stats:")
for k, v in comuna_stats.items():
    print(f"  {k}: {v}")

with open(COMMUNE_DIR / 'report.json', 'w') as f:
    json.dump(comuna_stats, f, ensure_ascii=False, indent=2)
with open(COMMUNE_DIR / 'updates.jsonl', 'w') as f:
    for u in commune_updates[:100]:
        f.write(json.dumps(u, ensure_ascii=False) + '\n')

# ===========================
# PART 3: APPLY CHANGES (if coherent)
# ===========================
print(f"\n{'='*80}")
print("PART 3: APPLYING CHANGES")
print("=" * 80)

# Apply commune updates
applied_commune = 0
for u in commune_updates:
    col.update_one(
        {'origen': 'toctoc', 'listing_id': u['listing_id']},
        {'$set': {
            'comuna': u['comuna'],
            'comuna_slug': u['comuna_slug'],
            'commune_source': u['source'],
            'updated_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        }}
    )
    applied_commune += 1

print(f"Commune updates applied: {applied_commune}")

# Apply INCIERTO classification changes (only explicit ones)
applied_class = 0
for c in changes:
    proposed = c['proposed']
    if proposed == 'INCIERTO':
        continue
    # Only apply changes with explicit textual evidence
    col.update_one(
        {'origen': 'toctoc', 'listing_id': c['listing_id']},
        {'$set': {
            'classification.state': proposed,
            'classification.reason': c['reason'],
            'classification.evidence': c['literal'],
            'classification.state_source': 'rules_audit',
            'updated_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        }}
    )
    applied_class += 1

print(f"Classification updates applied: {applied_class}")

# ===========================
# PART 4: VERIFICATION
# ===========================
print(f"\n{'='*80}")
print("VERIFICATION")
print("=" * 80)

post_t = col.count_documents({"origen": "toctoc"})
post_y = col.count_documents({"origen": "yapo"})
states = defaultdict(int)
commune_count = 0
for d in col.find({'origen': 'toctoc'}, {'classification.state': 1, 'comuna': 1, '_id': 0}):
    states[d.get('classification', {}).get('state', '?')] += 1
    if d.get('comuna', '') or '':
        commune_count += 1

print(f"Toctoc: {post_t}")
print(f"Yapo: {post_y} (changed={post_y != 5116})")
for k, v in sorted(states.items()):
    print(f"  {k}: {v}")
print(f"Docs with comuna: {commune_count}/{post_t} ({commune_count/post_t*100:.1f}%)")
