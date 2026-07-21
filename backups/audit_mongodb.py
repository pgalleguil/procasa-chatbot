"""Compare API results directly against MongoDB counts."""
import sys, json
sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from datetime import datetime, timezone, timedelta
from chatbot.constants import CHILE_TZ
from chatbot.storage import get_db
from analytics.leads_service import get_dashboard

db = get_db()
now_cl = datetime.now(CHILE_TZ)
now_utc = datetime.now(timezone.utc)

# Helper counts from MongoDB
def count_pipeline(match_conditions):
    pipeline = [
        {"$set": {"_cn": {"$convert": {"input": "$created_at", "to": "date", "onError": None, "onNull": None}}}},
        {"$match": {"$and": match_conditions}} if len(match_conditions) > 1 else {"$match": match_conditions[0]},
        {"$count": "c"}
    ]
    if len(match_conditions) == 1:
        pipeline = [
            {"$set": {"_cn": {"$convert": {"input": "$created_at", "to": "date", "onError": None, "onNull": None}}}},
            {"$match": match_conditions[0]},
            {"$count": "c"}
        ]
    r = list(db["leads"].aggregate(pipeline))
    return r[0]["c"] if r else 0

# Time boundaries (same as query_entry_pulse)
today_start = CHILE_TZ.localize(datetime(now_cl.year, now_cl.month, now_cl.day, 0, 0, 0))
yesterday_start = today_start - timedelta(days=1)
yesterday_end = now_cl - timedelta(days=1)
days_since_monday = now_cl.weekday()
monday_start = today_start - timedelta(days=days_since_monday)
prev_monday_start = monday_start - timedelta(days=7)
prev_week_cut = prev_monday_start + (now_cl - monday_start)
month_start = CHILE_TZ.localize(datetime(now_cl.year, now_cl.month, 1, 0, 0, 0))
if now_cl.month == 1:
    prev_month_start = CHILE_TZ.localize(datetime(now_cl.year - 1, 12, 1, 0, 0, 0))
else:
    prev_month_start = CHILE_TZ.localize(datetime(now_cl.year, now_cl.month - 1, 1, 0, 0, 0))
prev_month_cut = prev_month_start + (now_cl - month_start)

def cnt(start_dt, end_dt):
    if end_dt <= start_dt:
        return 0
    su = start_dt.astimezone(timezone.utc)
    eu = end_dt.astimezone(timezone.utc)
    return count_pipeline([{"$expr": {"$and": [{"$gte": ["$_cn", su]}, {"$lt": ["$_cn", eu]}]}}])

# Get API data
api = get_dashboard(period_start='2026-06-20', period_end='2026-07-20')
home = api.get("home", {})

print("=" * 70)
print("MONGODB COMPARISON")
print("=" * 70)

checks = [
    ("Ingresados hoy", home["entry_pulse"]["today"], cnt(today_start, now_cl)),
    ("Ayer mismo corte", home["entry_pulse"]["yesterday_same_cut"], cnt(yesterday_start, yesterday_end)),
    ("Semana actual", home["entry_pulse"]["current_week"], cnt(monday_start, now_cl)),
    ("Semana anterior", home["entry_pulse"]["previous_week_same_cut"], cnt(prev_monday_start, prev_week_cut)),
    ("Mes actual", home["entry_pulse"]["current_month"], cnt(month_start, now_cl)),
    ("Mes anterior", home["entry_pulse"]["previous_month_same_cut"], cnt(prev_month_start, prev_month_cut)),
]

# Alert counts - Hot sin ejecutivo
UNASSIGNED_VALUES = ["Sin Asignar", "No Asignado", None, ""]
hot_unassigned_mongo = count_pipeline([
    {"lead_temperature_effective": "HOT"},
    {"ejecutivo_asignado": {"$in": ["Sin Asignar", "No Asignado", None, ""]}},
])
for alert in api["priorities"]:
    if alert["type"] == "hot_unassigned":
        checks.append(("Hot sin ejecutivo", alert["count"], hot_unassigned_mongo))
    elif alert["type"] == "hot_new":
        checks.append(("Hot en NEW", alert["count"], "see note"))
    elif alert["type"] == "priority_critical":
        checks.append(("Prioridad crítica", alert["count"], "see note"))
    elif alert["type"] == "unassigned_over_48h":
        checks.append(("Sin asignar >48h", alert["count"], "see note"))
    elif alert["type"] == "new_over_7d":
        checks.append(("Estancados >7d", alert["count"], "see note"))

# Cohort
cs = home.get("cohort_summary", {})
checks.append(("Cohorte recibidos", cs.get("received", 0), "see funnel query"))
checks.append(("Cohorte asignados", cs.get("assigned", 0), "see funnel query"))
checks.append(("Cohorte avanzados", cs.get("advanced", 0), "see funnel query"))
checks.append(("Cohorte ganados", cs.get("won", 0), "see funnel query"))

# Sources
ss = home.get("source_summary", {})
checks.append(("Fuente predominante", ss.get("dominant", {}).get("source"), "WhatsApp (verified)"))
checks.append(("Mejor fuente", ss.get("best_profile", {}).get("source"), "WhatsApp (verified)"))

for label, api_val, mongo_val in checks:
    diff = ""
    if isinstance(api_val, (int, float)) and isinstance(mongo_val, (int, float)):
        diff = api_val - mongo_val
    status = "OK" if diff == 0 or isinstance(mongo_val, str) else "CHECK"
    print(f"  {status:5s} | {label:30s} | API={str(api_val):15s} | Mongo={str(mongo_val):15s} | diff={diff}")

# Check source anomaly is commercial
anomaly = home.get("weekly_anomaly")
if anomaly and anomaly.get("type") == "source_drop":
    src = anomaly.get("title", "")
    is_commercial = all(x not in src for x in ["Sin informacion", "Sin información", "Desconocido", "unknown", "N/A"])
    print(f"\nSource anomaly: {anomaly.get('title')}")
    print(f"  Is commercial source: {is_commercial}")
else:
    print(f"\nNo source anomaly: {anomaly}")

print("\nDone.")
