"""Audit timezone handling and forecast calculation."""
import sys, json
sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from datetime import datetime, timezone, timedelta
from chatbot.constants import CHILE_TZ
from analytics.leads_queries import query_entry_pulse, query_entry_forecast, query_comparative_trends

now_utc = datetime.now(timezone.utc)
now_cl = datetime.now(CHILE_TZ)

print("=" * 60)
print("TIMEZONE AUDIT")
print("=" * 60)
print(f"Server UTC time:      {now_utc.isoformat()}")
print(f"America/Santiago:     {now_cl.isoformat()}")
print(f"UTC offset:           {now_cl.utcoffset()}")
print(f"Chile TZ name:        {now_cl.tzname()}")

# Test query_entry_pulse boundaries
pulse = query_entry_pulse()
print(f"\nEntry pulse result:")
print(f"  today: {pulse['today']}")
print(f"  yesterday_same_cut: {pulse['yesterday_same_cut']}")
print(f"  current_week: {pulse['current_week']}")
print(f"  previous_week_same_cut: {pulse['previous_week_same_cut']}")
print(f"  current_month: {pulse['current_month']}")
print(f"  previous_month_same_cut: {pulse['previous_month_same_cut']}")

# Verify boundaries manually
now_cl = datetime.now(CHILE_TZ)
today_start = CHILE_TZ.localize(datetime(now_cl.year, now_cl.month, now_cl.day, 0, 0, 0))
yesterday_start = today_start - timedelta(days=1)
yesterday_end = now_cl - timedelta(days=1)
print(f"\nManual boundary verification:")
print(f"  now_cl:               {now_cl.isoformat()}")
print(f"  today_start (CL):     {today_start.isoformat()}")
print(f"  today_start (UTC):    {today_start.astimezone(timezone.utc).isoformat()}")
print(f"  yesterday_start (CL): {yesterday_start.isoformat()}")
print(f"  yesterday_end (CL):   {yesterday_end.isoformat()}")

# Week boundaries
days_since_monday = now_cl.weekday()
monday_start = today_start - timedelta(days=days_since_monday)
prev_monday_start = monday_start - timedelta(days=7)
prev_week_cut = prev_monday_start + (now_cl - monday_start)
print(f"\n  monday_start (CL):    {monday_start.isoformat()}")
print(f"  prev_monday (CL):     {prev_monday_start.isoformat()}")
print(f"  prev_week_cut (CL):   {prev_week_cut.isoformat()}")
print(f"  days_since_monday:    {days_since_monday}")

# Month boundaries
month_start = CHILE_TZ.localize(datetime(now_cl.year, now_cl.month, 1, 0, 0, 0))
if now_cl.month == 1:
    prev_month_start = CHILE_TZ.localize(datetime(now_cl.year - 1, 12, 1, 0, 0, 0))
else:
    prev_month_start = CHILE_TZ.localize(datetime(now_cl.year, now_cl.month - 1, 1, 0, 0, 0))
prev_month_cut = prev_month_start + (now_cl - month_start)
print(f"\n  month_start (CL):     {month_start.isoformat()}")
print(f"  prev_month (CL):      {prev_month_start.isoformat()}")
print(f"  prev_month_cut (CL):  {prev_month_cut.isoformat()}")

print("\n" + "=" * 60)
print("FORECAST AUDIT")
print("=" * 60)
from chatbot.storage import get_db
from analytics.leads_queries import _normalized_created_at_stage, _format_date_field

db = get_db()
now_cl = datetime.now(CHILE_TZ)
start_60d = (now_cl - timedelta(days=60)).replace(hour=0, minute=0, second=0, microsecond=0)
su = start_60d.astimezone(timezone.utc)
eu = now_cl.astimezone(timezone.utc)

print(f"Forecast period start (UTC): {su.isoformat()}")
print(f"Forecast period end (UTC):   {eu.isoformat()}")

# Get raw daily data
pipeline = [
    _normalized_created_at_stage(),
    {"$match": {"$expr": {"$and": [{"$gte": ["$_created_normalized", su]}, {"$lt": ["$_created_normalized", eu]}]}}},
    {"$group": {"_id": _format_date_field("$_created_normalized"), "count": {"$sum": 1}}},
    {"$sort": {"_id": 1}},
]
rows = list(db["leads"].aggregate(pipeline))
print(f"\nTotal days in history: {len(rows)}")
print(f"First date: {rows[0]['_id']}")
print(f"Last date: {rows[-1]['_id']}")

daily = {r["_id"]: r["count"] for r in rows}
total_leads = sum(daily.values())
print(f"Total leads in 60d window: {total_leads}")
print(f"Avg daily: {total_leads / len(rows):.1f}")

# Last 7 complete days (exclude today if incomplete)
dates = sorted(daily.keys())
today_str = now_cl.strftime("%Y-%m-%d")
complete_dates = [d for d in dates if d < today_str]
last_7 = complete_dates[-7:] if len(complete_dates) >= 7 else dates[-7:]
recent_total = sum(daily.get(d, 0) for d in last_7)
avg_7 = recent_total / len(last_7)
print(f"\nLast 7 COMPLETE days ({last_7[0]} to {last_7[-1]}): total={recent_total}, avg={avg_7:.1f}")

# Day-of-week factors
overall_avg = sum(daily.values()) / len(daily) if daily else 1
from collections import defaultdict
dow_counts = defaultdict(list)
for date_str, count in daily.items():
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    dow = dt.weekday()
    dow_counts[dow].append(count)

dow_names = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]
print(f"\nDay-of-week analysis (overall avg={overall_avg:.1f}):")
dow_factors = {}
for dow in range(7):
    counts = dow_counts.get(dow, [0])
    avg = sum(counts) / len(counts) if counts else 0
    factor = avg / overall_avg if overall_avg else 1
    dow_factors[dow] = factor
    print(f"  {dow_names[dow]}: avg={avg:.1f}, factor={factor:.2f}, n={len(counts)}")

# Project next 7 days
print(f"\nProjected next 7 days (from {today_str}):")
projected = []
for i in range(1, 8):
    fd = now_cl + timedelta(days=i)
    dow = fd.weekday()
    factor = dow_factors.get(dow, 1)
    val = round(avg_7 * factor)
    projected.append(val)
    print(f"  {fd.strftime('%a %Y-%m-%d')}: base={avg_7:.1f} * factor={factor:.2f} = {val}")

total_est = sum(projected)
band = round(total_est * 0.25)
print(f"\nTotal projected: {total_est}")
print(f"Band (25%): {band}")
print(f"Range: {max(0, total_est - band)}-{total_est + band}")

# Check if "today" is correctly excluded
today_count = daily.get(today_str, 0)
print(f"\nToday ({today_str}) count in window: {today_count}")
print(f"Is today excluded from 7-day avg? {'Yes' if today_str not in complete_dates else 'No'}")
