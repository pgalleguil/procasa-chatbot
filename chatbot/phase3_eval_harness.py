"""Pure helpers for reproducible, causal Phase 3 evaluation (no external I/O)."""
from __future__ import annotations
import re
from datetime import datetime, timezone

_LINE = re.compile(r"^\[(?P<time>\d{1,2}:\d{2})(?:,?\s*(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}))?\]\s*(?P<actor>CLIENTE|PROCASA|BOT|ASESOR|EJECUTIV[OA]):\s*(?P<text>.*)$", re.I)

def parse_exported_transcript(text: str, *, default_date=None):
    events, current = [], None
    for line in str(text or "").splitlines():
        match = _LINE.match(line.strip())
        if match:
            if current: events.append(current)
            actor = match['actor'].upper()
            role = 'user' if actor == 'CLIENTE' else 'assistant'
            stamp = f"{match['date']} {match['time']}" if match['date'] else match['time']
            timestamp = None
            if match['date']:
                try:
                    timestamp = datetime.strptime(stamp, '%d/%m/%Y %H:%M').replace(tzinfo=timezone.utc)
                except ValueError:
                    try: timestamp = datetime.strptime(stamp, '%d-%m-%Y %H:%M').replace(tzinfo=timezone.utc)
                    except ValueError: timestamp = None
            current = {'timestamp_raw': stamp, 'timestamp': timestamp, 'role': role, 'actor': actor, 'content': match['text'].strip(), 'dataset_artifact': True}
        elif current:
            current['content'] = (current['content'] + "\n" + line.strip()).strip()
    if current: events.append(current)
    return events

def causal_facts(property_doc: dict | None, *, turn_at, reference_known_at=None, resolution_error=False):
    """Classify an absent historical fact without disguising lookup failures."""
    if resolution_error: return {}, 'PROPERTY_RESOLUTION_FAILED'
    if not property_doc: return {}, 'HISTORICAL_DATA_MISSING'
    created = property_doc.get('created_at') or property_doc.get('first_seen_at')
    updated = property_doc.get('updated_at') or property_doc.get('last_full_sync')
    if reference_known_at and turn_at and reference_known_at > turn_at: return {}, 'FACT_NOT_AVAILABLE_AT_TIME'
    if created and turn_at and created > turn_at: return {}, 'FACT_NOT_AVAILABLE_AT_TIME'
    if not updated: return {}, 'HISTORICAL_DATA_MISSING'
    keys = ('precio_uf','precio_clp','gastos_comunes','dormitorios','banos','estacionamientos','superficie_util','superficie_total','comuna','tipo','operacion','orientacion')
    facts={k:property_doc[k] for k in keys if property_doc.get(k) not in (None,'')}
    return facts, 'FACT_CAUSALLY_AVAILABLE' if facts else 'HISTORICAL_DATA_MISSING'

def fake_search(properties, criteria, *, allow_relaxation=False):
    """Strict first, then explicitly requested near matches; never mutates criteria."""
    criteria = dict(criteria)
    def match(p, relax=False):
        for key in ('operation','comuna','tipo'):
            if criteria.get(key) and str(p.get(key,'')).casefold()!=str(criteria[key]).casefold(): return False
        for key in ('dormitorios','banos','estacionamientos'):
            if criteria.get(key) is not None and p.get(key,0) < criteria[key]: return False
        for key, price_key in (('budget_clp','precio_clp'),('budget_uf','precio_uf')):
            budget=criteria.get(key)
            if budget is not None and (p.get(price_key) is None or p[price_key] > budget*(1.15 if relax else 1)): return False
        return True
    exact=[p for p in properties if match(p)]
    near = [p for p in properties if match(p,True)] if allow_relaxation else []
    return {'status':'exact','results':exact,'criteria':criteria,'relaxed':False} if exact else {'status':'near' if near else 'none','results':near,'criteria':criteria,'relaxed':bool(near)}

class StatefulReplay:
    """In-memory evaluator state; deliberately independent from production DB."""
    def __init__(self):
        self.property_id=None; self.operation=None; self.criteria={}; self.pending_questions=[]
        self.visit_state='NONE'; self.visit_property_id=None; self.owner='bot'; self.handoff='NONE'
        self.answered=set(); self.temperature='COLD'; self.history=[]; self.search_relaxation_offered=False
    def apply_reference(self, *, property_id=None, operation=None):
        if property_id is not None and property_id != self.property_id:
            self.property_id=property_id; self.visit_state='NONE'; self.visit_property_id=None
        if operation and operation != self.operation:
            self.operation=operation; self.criteria={**self.criteria,'operation':operation}
    def apply_search(self, **criteria):
        self.criteria.update({k:v for k,v in criteria.items() if v not in (None,'')})
    def add_questions(self, questions):
        self.pending_questions.extend(q for q in questions if q not in self.answered and q not in self.pending_questions)
    def resolve(self, question, *, available):
        if question in self.pending_questions: self.pending_questions.remove(question)
        self.answered.add(question)
        return 'answered' if available else 'explicitly_unknown'
    def capture_visit(self, preference=None):
        self.visit_state='PREFERENCE_CAPTURED' if preference else 'INTEREST_DETECTED'
        self.visit_property_id=self.property_id; self.temperature='HOT'
    def handoff_human(self):
        self.handoff='EXISTS'; self.temperature='HOT'
    def human_takeover(self):
        self.owner='human'; self.handoff='EXISTS'
    def may_send(self): return self.owner != 'human'
