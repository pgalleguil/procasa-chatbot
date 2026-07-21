"""Detailed forecast audit."""
import sys
sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from analytics.leads_queries import query_entry_forecast, query_entry_pulse
from datetime import datetime, timedelta
from chatbot.constants import CHILE_TZ

fc = query_entry_forecast()
pulse = query_entry_pulse()

print("PULSE DATA:")
print(f"  Today: {pulse['today']}")
print(f"  Yesterday same cut: {pulse['yesterday_same_cut']}")
print(f"  Current week: {pulse['current_week']}")
print(f"  Previous week same cut: {pulse['previous_week_same_cut']}")
print(f"  Current month: {pulse['current_month']}")
print(f"  Previous month same cut: {pulse['previous_month_same_cut']}")

if fc:
    print("\nFORECAST DATA:")
    print(f"  Days of history: {fc['days_of_history']}")
    print(f"  Days with leads: {fc['days_with_leads']}")
    print(f"  Days with zero: {fc['days_with_zero']}")
    total = fc['days_with_leads'] + fc['days_with_zero']
    print(f"  Total calendar days: {total}")
    print(f"  Total leads in window: {fc['total_leads_in_window']}")
    print(f"  Overall avg (60d): {fc['overall_avg_60d']}")
    print(f"  Recent 7d avg: {fc['recent_7d_avg']}")
    print(f"  Projected daily: {fc['projected_daily']}")
    print(f"  Total projected: {sum(fc['projected_daily'])}")
    r = fc['total_range']
    print(f"  Range: {r['min']}-{r['max']}")
    print(f"  Band: {r['max'] - sum(fc['projected_daily'])} (25%)")
    print(f"  Method: {fc['method']}")
else:
    print("Forecast: None (insufficient data)")
