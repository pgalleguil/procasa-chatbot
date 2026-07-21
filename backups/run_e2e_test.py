"""End-to-end test: starts server, tests endpoints, captures results."""
# -*- coding: utf-8 -*-
import sys, json, os, subprocess, time, signal
sys.path.insert(0, r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')

from jose import jwt
from config import Config
from datetime import datetime, timedelta, timezone
import urllib.request, http.cookiejar

PORT = 8080
BASE = f"http://localhost:{PORT}"

# Start server
server_dir = r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok'
server = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'webhook:app', '--host', '127.0.0.1', '--port', str(PORT), '--log-level', 'error'],
    cwd=server_dir,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL
)
print(f"Server PID: {server.pid}")
time.sleep(18)

results = {"passed": 0, "failed": 0, "details": []}

def check(name, condition, detail=""):
    if condition:
        results["passed"] += 1
        results["details"].append({"test": name, "status": "PASS", "detail": detail})
        print(f"  ✓ {name}")
    else:
        results["failed"] += 1
        results["details"].append({"test": name, "status": "FAIL", "detail": detail})
        print(f"  ✗ {name}: {detail}")

# Create auth token
token = jwt.encode(
    {"sub": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=2)},
    Config.SECRET_KEY, algorithm="HS256"
)

# Use Authorization header instead of cookies (more reliable for testing)
opener = urllib.request.build_opener()
opener.addheaders = [("Authorization", f"Bearer {token}")]

# 1. API Dashboard
try:
    req = urllib.request.Request(f"{BASE}/api/analytics/leads/dashboard?period_start=2026-06-20&period_end=2026-07-20")
    resp = opener.open(req)
    check("GET /api/analytics/leads/dashboard", resp.status == 200, f"HTTP {resp.status}")
    data = json.loads(resp.read())
    home = data.get("home", {})
    check("Payload has home section", bool(home), "")
    check("Headline exists", bool(home.get("headline")), f"title={home.get('headline',{}).get('title','')}")
    check("Entry pulse exists", bool(home.get("entry_pulse")), f"today={home['entry_pulse']['today']}")
    check("Top alerts (max 3)", len(home.get("top_alerts", [])) <= 3, f"count={len(home.get('top_alerts',[]))}")
    check("Cohort summary exists", bool(home.get("cohort_summary")), f"received={home['cohort_summary']['received']}")
    check("Source summary exists", bool(home.get("source_summary")), f"dominant={home['source_summary'].get('dominant',{}).get('source','')}")
    anomaly = home.get("weekly_anomaly")
    check("Anomaly valid (commercial source or null)", 
          anomaly is None or (anomaly.get("type") != "source_drop" or anomaly.get("title","").find("Sin informacion") == -1),
          f"anomaly={anomaly.get('title','None') if anomaly else 'None'}")
    forecast = home.get("entry_forecast")
    check("Forecast valid or null", forecast is None or (forecast.get("total_range",{}).get("min",0) > 0),
          f"range={forecast.get('total_range',{}) if forecast else 'None'}")
    # Check existing keys preserved
    for key in ["status_strip", "priorities", "cohort_status", "executive_load", 
                "source_performance", "demand", "property_ranking", "coverage",
                "unavailable_metrics", "roadmap_capabilities"]:
        check(f"Key '{key}' preserved", key in data, "")
except Exception as e:
    check("API dashboard request", False, str(e))

# 2. Filters
try:
    req = urllib.request.Request(f"{BASE}/api/analytics/leads/filters")
    resp = opener.open(req)
    check("GET /api/analytics/leads/filters", resp.status == 200, f"HTTP {resp.status}")
    fj = json.loads(resp.read())
    check("Filters has executives", len(fj.get("executives", [])) > 0, f"count={len(fj['executives'])}")
    check("Filters has sources", len(fj.get("sources", [])) > 0, f"count={len(fj['sources'])}")
except Exception as e:
    check("Filters request", False, str(e))

# 3. Analytics page HTML
try:
    req = urllib.request.Request(f"{BASE}/analytics/leads")
    resp = opener.open(req)
    check("GET /analytics/leads", resp.status == 200, f"HTTP {resp.status}")
    html = resp.read().decode('utf-8', errors='replace')
    for label, check_str in [
        ("Tab: Inicio", "Inicio"),
        ("Tab: Cartera y prioridades", "Cartera y prioridades"),
        ("Tab: Demanda", "Demanda y propiedades"),
        ("Tab: Calidad de datos", "Calidad de datos"),
        ("Section: home-view", "home-view"),
        ("Section: operation-view", "operation-view"),
        ("Section: demand-view", "demand-view"),
        ("Section: data-quality-view", "data-quality-view"),
        ("Component: homeHeadline", "homeHeadline"),
        ("Component: homePulse", "homePulse"),
        ("Component: homeAlerts", "homeAlerts"),
        ("Component: homeCohort", "homeCohort"),
        ("Component: homeSources", "homeSources"),
        ("Component: head headline-block", "headline-block"),
        ("Component: roadmapCapabilities", "roadmapCapabilities"),
    ]:
        check(f"HTML contains {label}", check_str in html, f"looking for '{check_str}'")
except Exception as e:
    check("Analytics page request", False, str(e))

# 4. Filter test: by executive
try:
    req = urllib.request.Request(f"{BASE}/api/analytics/leads/dashboard?period_start=2026-06-20&period_end=2026-07-20&executive=Paulina")
    resp = opener.open(req)
    check("Filter by executive", resp.status == 200, f"HTTP {resp.status}")
    d2 = json.loads(resp.read())
    check("  home present with filter", bool(d2.get("home")), "")
except Exception as e:
    check("Filter by executive", False, str(e))

# 5. Cleanup - diff audit
import subprocess as sp
diff_check = sp.run(["git", "diff", "--check"], capture_output=True, text=True, cwd=server_dir)
check("git diff --check clean", diff_check.returncode == 0, diff_check.stdout[:200] if diff_check.stdout else "ok")

diff_stat = sp.run(["git", "diff", "--stat"], capture_output=True, text=True, cwd=server_dir)
print(f"\n--- git diff --stat ---\n{diff_stat.stdout}")

diff_numstat = sp.run(["git", "diff", "--numstat"], capture_output=True, text=True, cwd=server_dir)
print(f"\n--- git diff --numstat ---\n{diff_numstat.stdout}")

# Summary
print(f"\n{'='*50}")
print(f"RESULTS: {results['passed']} passed, {results['failed']} failed")
print(f"{'='*50}")

# Stop server
server.terminate()
server.wait(timeout=5)
print("Server stopped.")
