import json
import os

with open('audit_reclassification_data.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

records = data['records']
changed_records = [r for r in records if r['changed']]

print("=== 1. MUESTRA DE 20 CASOS ===")
for r in changed_records[:20]:
    print(f"ID: {r['doc_id']}")
    print(f"URL: {r['url']}")
    print(f"publicador: {r['signals']['seller_name']}")
    print(f"seller_is_pro: {r['signals']['seller_is_pro']}")
    print(f"broker_brand: {r['signals']['broker_brand']}")
    print(f"company_name: {r['signals']['company_name']}")
    print(f"broker_score: {r['recalculated']['score_corredor']}")
    print(f"owner_score: {r['recalculated']['score_dueno']}")
    print(f"Almacenado: {r['stored']['classification_state']}")
    print(f"Recalculado: {r['recalculated']['classification_state']}")
    print("-" * 40)

print("\n=== 3. DUEÑO -> CORREDOR (Señales) ===")
d2c = [r for r in changed_records if r['recalculated']['classification_state'] == 'CORREDOR_SEGURO']
for r in d2c:
    signals = []
    if r['signals']['seller_is_pro']: signals.append("seller_is_pro=True")
    if r['signals']['broker_brand'] != 'N/A': signals.append(f"broker_brand='{r['signals']['broker_brand']}'")
    if r['signals']['company_name'] != 'N/A': signals.append(f"company_name='{r['signals']['company_name']}'")
    print(f"{r['doc_id']}: {', '.join(signals)} (score_corredor: {r['recalculated']['score_corredor']})")

print("\n=== 4. DUEÑO -> INCIERTO ===")
d2i = [r for r in changed_records if r['recalculated']['classification_state'] == 'INCIERTO']
for r in d2i:
    print(f"{r['doc_id']}: score_dueno={r['recalculated']['score_dueno']}, score_corredor={r['recalculated']['score_corredor']}, seller_is_pro={r['signals']['seller_is_pro']}")

print("\n=== 5. RIESGO COMPANY_NAME CONTAMINADO ===")
for r in changed_records:
    if r['signals']['company_name'] != 'N/A' and r['recalculated']['classification_state'] == 'CORREDOR_SEGURO':
        print(f"ID: {r['doc_id']} | publicador: {r['signals']['seller_name']} | company_name: {r['signals']['company_name']} | broker_brand: {r['signals']['broker_brand']}")
