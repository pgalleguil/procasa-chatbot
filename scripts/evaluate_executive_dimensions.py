"""Synthetic requirement extraction, transitions, attribution and contact metrics."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests/fixtures/executive_intelligence'))
from catalog import turn
from chatbot.executive_conversation_intelligence import classify_hybrid
from scripts.evaluate_executive_intelligence import metrics

# Exact labels authored independently of observed output. Unsupported fields
# intentionally remain in this evaluation and count as omissions, not successes.
EXTRACTION = [
 ('Busco comprar una casa en Maipú hasta 3500 UF', {'DEMAND': {'operation': 'VENTA', 'property_type': 'CASA', 'comunas': ['Maipú'], 'max_price_uf': 3500}}),
 ('Busco arriendo de departamento en Peñaflor hasta 700000 CLP', {'DEMAND': {'operation': 'ARRIENDO', 'property_type': 'DEPARTAMENTO', 'comunas': ['Peñaflor'], 'max_price_clp': 700000}}),
 ('Busco casa de 3 dormitorios con 2 baños y 1 estacionamiento', {'DEMAND': {'property_type': 'CASA', 'bedrooms': 3, 'bathrooms': 2, 'parking': 1}}),
 ('Buscaba hasta 3.000 UF, pero ahora puedo llegar a 3.500 UF', {'DEMAND': {'max_price_uf': 3500}}),
 ('Quiero vender mi casa en Peñaflor y después comprar un departamento en Maipú', {'OWNER_SELL': {'operation': 'VENTA', 'property_type': 'CASA', 'comunas': ['Peñaflor']}, 'DEMAND': {'operation': 'VENTA', 'property_type': 'DEPARTAMENTO', 'comunas': ['Maipú']}}),
 ('Quiero arrendar mi oficina en Santiago', {'OWNER_RENT': {'operation': 'ARRIENDO', 'property_type': 'OFICINA', 'comunas': ['Santiago']}}),
 ('Busco comprar un terreno entre 2000 y 3500 UF', {'DEMAND': {'operation': 'VENTA', 'property_type': 'TERRENO', 'min_price_uf': 2000, 'max_price_uf': 3500}}),
 ('Necesito financiamiento', {'DEMAND': {'financing_needed': True}}),
 ('Busco casa urgente', {'DEMAND': {'property_type': 'CASA', 'urgency': 'EXPLICIT'}}),
 ('Quiero comprar la propiedad código 12345', {'DEMAND': {'operation': 'VENTA', 'property_reference': '12345'}}),
 ('No busco en Maipú', {}),
 ('Me interesa', {}),
 ('Busco casa en venta', {'DEMAND': {'operation': 'VENTA', 'property_type': 'CASA'}}),
 ('Busco casa con jardín', {'DEMAND': {'property_type': 'CASA', 'garden': True}}),
 ('Busco departamento en sector Ciudad Satélite', {'DEMAND': {'property_type': 'DEPARTAMENTO', 'sector': 'Ciudad Satélite'}}),
 ('Busco casa hasta cien millones de pesos', {'DEMAND': {'property_type': 'CASA', 'max_price_clp': 100000000}}),
]

SCENARIOS = [
 ('customer_before_outreach', [('customer', 'Hola'), ('human_agent', 'Hola')], 'NO_SIGNAL', 'UNKNOWN', 'UNKNOWN', False, True, 'CUSTOMER'),
 ('reply_after_outreach', [('human_agent', 'Hola'), ('customer', 'Quiero agendar una visita')], 'ASK_VISIT', 'ACTIVE', 'UNKNOWN', True, True, 'EXECUTIVE'),
 ('agent_only', [('human_agent', 'Quiero agendar una visita')], 'NO_SIGNAL', 'UNKNOWN', 'UNKNOWN', False, True, 'CUSTOMER'),
 ('customer_only', [('customer', 'Quiero agendar una visita')], 'ASK_VISIT', 'ACTIVE', 'UNKNOWN', False, False, 'EXECUTIVE'),
 ('decline_alternative', [('customer', 'Esa casa no me interesa, pero ¿tienen otra parecida?')], 'ASK_ALTERNATIVES', 'ACTIVE', 'UNKNOWN', False, False, 'EXECUTIVE'),
 ('close_then_greeting', [('customer', 'Ya compré una casa'), ('customer', 'Hola')], 'NO_SIGNAL', 'COMPLETED', 'UNKNOWN', False, False, 'NONE'),
 ('stop_then_greeting', [('customer', 'No me contacten nunca más'), ('customer', 'Hola')], 'NO_SIGNAL', 'UNKNOWN', 'OPTED_OUT', False, False, 'EXECUTIVE'),
 ('reactivate', [('customer', 'Ya encontré departamento'), ('customer', 'Ahora necesito una casa para arrendar')], 'SEARCH_UPDATED', 'ACTIVE', 'UNKNOWN', False, False, 'EXECUTIVE'),
 ('reactivate_without_consent', [('customer', 'No me contacten más'), ('customer', 'Ahora necesito una casa para arrendar')], 'SEARCH_UPDATED', 'ACTIVE', 'OPTED_OUT', False, False, 'EXECUTIVE'),
 ('elliptical_visit', [('human_agent', '¿Quiere que agendemos una visita?'), ('customer', 'Sí, el sábado podría')], 'ASK_VISIT', 'ACTIVE', 'UNKNOWN', True, True, 'EXECUTIVE'),
 ('ambiguous_choice', [('human_agent', '¿Prefiere una visita o una llamada?'), ('customer', 'Sí, gracias')], 'NO_SIGNAL', 'UNKNOWN', 'UNKNOWN', True, True, 'EXECUTIVE'),
 ('executive_offer_not_customer_offer', [('human_agent', 'Mi oferta es 3000 UF'), ('customer', 'Hola')], 'NO_SIGNAL', 'UNKNOWN', 'UNKNOWN', True, True, 'EXECUTIVE'),
]


def facts(needs):
 return {(role + '.' + key, json.dumps(value, sort_keys=True, ensure_ascii=False))
         for role, fields in needs.items() for key, value in fields.items()}


def main():
 extraction = []
 for number, (text, expected) in enumerate(EXTRACTION):
  for context in (False, True):
   messages = ([turn('human_agent', 'Tengo una casa de 4 dormitorios en Las Condes a 9000 UF', 0)] if context else []) + [turn('customer', text, 1)]
   result = classify_hybrid(messages, llm_enabled=False)
   truth, observed = facts(expected), facts(result['needs'])
   extraction.append(dict(id=f'extraction-{number}-{context}', tp=len(truth & observed),
                          fp=len(observed-truth), fn=len(truth-observed),
                          missing=sorted(truth-observed), unexpected=sorted(observed-truth)))
 counts = {key: sum(row[key] for row in extraction) for key in ('tp', 'fp', 'fn')}
 tp, fp, fn = counts['tp'], counts['fp'], counts['fn']
 counts.update(precision=tp/(tp+fp), recall=tp/(tp+fn), f1=2*tp/(2*tp+fp+fn))
 dimensions = {key: [] for key in ('intent', 'search_status', 'contact_permission', 'customer_responded', 'agent_contacted', 'awaiting')}
 for name, conversation, *expected in SCENARIOS:
  ordered = [turn(actor, text, i) for i, (actor, text) in enumerate(conversation)]
  for variant, messages in [('ordered', ordered), ('out_of_order', list(reversed(ordered))), ('duplicate', ordered + ordered)]:
   result = classify_hybrid(messages, llm_enabled=False)
   actual = [result['intent'], result['search_status'], result['contact_permission'], result['contact']['customer_responded'], result['contact']['agent_contacted'], result['contact']['awaiting']]
   for key, truth, observed in zip(dimensions, expected, actual):
    dimensions[key].append(dict(id=name+'-'+variant, expected=str(truth), predicted=str(observed)))
 report = dict(mode='deterministic_only', extraction_cases=len(extraction), extraction_metrics=counts,
               extraction_rows=extraction, scenario_cases=len(SCENARIOS)*3,
               dimensions={key: metrics(rows) for key, rows in dimensions.items()},
               scenario_rows=dimensions,
               limitations='Synthetic; variants correlated; extraction measures exact field/value facts; no LLM extraction accuracy claim.')
 output=ROOT/'reports/executive_intelligence/dimensions.json'
 output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
 print(json.dumps(dict(extraction=counts, dimensions={k:v['macro_f1'] for k,v in report['dimensions'].items()})))


if __name__ == '__main__':
 main()
