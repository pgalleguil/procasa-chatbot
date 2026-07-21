"""Audit MercadoLibre anomaly with exact window comparison."""
import sys
sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from datetime import datetime, timezone, timedelta
from chatbot.constants import CHILE_TZ
from analytics.leads_queries import query_entry_pulse, query_source_performance

# Get source performance for the period
sources = query_source_performance(period_start='2026-06-20', period_end='2026-07-20')
mercadolibre = None
for s in sources:
    if s['source'] == 'MercadoLibre':
        mercadolibre = s
        break

if mercadolibre:
    print("=" * 60)
    print("MERCADOLIBRE ANOMALY AUDIT")
    print("=" * 60)
    print(f"Source: {mercadolibre['source']}")
    print(f"Received (current period): {mercadolibre['received']}")
    print(f"Variation vs previous period: {mercadolibre['variation_pct']}%")
    print(f"Hot %: {mercadolibre['hot_pct']}%")
    print(f"Assigned %: {mercadolibre['assigned_pct']}%")
    print(f"Advanced %: {mercadolibre['advanced_pct']}%")
    
    # The comparison is between 30-day windows
    period_start = datetime.fromisoformat('2026-06-20')
    period_end = datetime.fromisoformat('2026-07-20')
    duration = (period_end - period_start).days
    print(f"\nPeriod comparison:")
    print(f"  Current: {period_start.date()} to {period_end.date()} ({duration} days)")
    print(f"  Previous: {(period_start - timedelta(days=duration)).date()} to {period_start.date()} ({duration} days)")
    
    # Check if the drop is >= 30%
    var = mercadolibre.get('variation_pct')
    if var is not None and var < -30:
        print(f"\n  CONDITION MET: {var}% < -30%")
        print(f"  Anomaly SHOULD be triggered")
    else:
        print(f"\n  CONDITION NOT MET: {var}% >= -30%")
        print(f"  Anomaly should NOT be triggered")
else:
    print("MercadoLibre not found in source performance data")
    for s in sources:
        print(f"  {s['source']}: {s['received']} leads, var={s.get('variation_pct')}%")

print("\nAll commercial sources in period:")
for s in sources:
    print(f"  {s['source']:30s} received={s['received']:3d} var={str(s.get('variation_pct','N/A')):>8s} hot={s['hot_pct']}% adv={s['advanced_pct']}%")
