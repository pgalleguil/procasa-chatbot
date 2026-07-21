"""Inject home view CSS into analytics-dashboard.css - read, insert, write."""
import sys, re

css_path = r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\static\css\analytics-dashboard.css'

with open(css_path, 'r', encoding='utf-8') as f:
    content = f.read()

home_styles = """
.home-headline{padding:24px 26px}.headline-block{display:flex;align-items:flex-start;gap:18px}.headline-icon{width:44px;height:44px;border-radius:50%;display:grid;place-items:center;flex:none}.headline-icon i{font-size:20px;color:#fff}.headline-content{flex:1;min-width:0}.headline-severity-label{font-size:10px;font-weight:700;letter-spacing:.12em;margin-bottom:3px}.headline-critical .headline-severity-label{color:var(--risk)}.headline-warning .headline-severity-label{color:var(--amber)}.headline-positive .headline-severity-label{color:var(--ok)}.headline-neutral .headline-severity-label{color:var(--muted)}.headline-title{font-size:18px;line-height:1.25;display:block;margin-bottom:4px}.headline-detail{font-size:12px;margin:0}.headline-action{flex:none;padding:8px 16px;background:var(--brand-soft);color:var(--brand);border-radius:10px;text-decoration:none;font-weight:600;font-size:12px;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}.headline-action:hover{background:var(--brand);color:#fff}
.home-metrics-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.pulse-header{display:grid;grid-template-columns:minmax(80px,1fr) 64px 80px 72px 64px 80px;gap:6px;padding:6px 0;color:var(--muted);font-size:10px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;border-bottom:1px solid var(--line)}.pulse-row{display:grid;grid-template-columns:minmax(80px,1fr) 64px 80px 72px 64px 80px;gap:6px;align-items:center;min-height:34px;border-bottom:1px solid var(--line);font-size:12px}.pulse-row:last-child{border-bottom:0}.pulse-label{font-weight:600}.pulse-value{font-weight:700;font-size:15px}.pulse-compare{color:var(--muted);font-size:11px}.pulse-trend{font-weight:700;font-size:12px}.pulse-trend.up{color:var(--ok)}.pulse-trend.down{color:var(--risk)}.pulse-month{border-top:1px solid var(--line);margin-top:4px;padding-top:4px;color:var(--muted)}
.alert-row{display:grid;grid-template-columns:18px 42px 1fr;align-items:center;gap:10px;min-height:38px;padding:4px 0;border-bottom:1px solid var(--line);font-size:12px}.alert-row:last-child{border-bottom:0}.alert-icon{font-size:14px;text-align:center}.alert-count{font-weight:700;font-size:17px}.alert-label{color:var(--muted)}.home-empty-state{padding:14px 0;color:var(--muted);font-size:12px;text-align:center}
.cohort-home-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}.cohort-home-stat{text-align:center;padding:10px;background:var(--surface-2);border-radius:10px}.cohort-home-stat strong{display:block;font-size:22px;font-weight:700}.cohort-home-stat span{color:var(--muted);font-size:10px;display:block;margin-top:2px}.cohort-home-drop{padding:8px 10px;background:var(--surface-2);border-radius:10px;font-size:11px;color:var(--muted);margin-bottom:8px;text-align:center}.cohort-home-drop strong{color:var(--text)}
.sources-home-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}.source-home-card{padding:12px;background:var(--surface-2);border-radius:12px}.source-home-label{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px}.source-home-card strong{font-size:14px;display:block;margin-bottom:2px}.source-home-count{color:var(--muted);font-size:11px}
.home-link{color:var(--brand);text-decoration:none;font-size:12px;font-weight:600;display:inline-flex;align-items:center;gap:5px}.home-link:hover{text-decoration:underline}
.anomaly-card{display:flex;align-items:flex-start;gap:12px;padding:12px;border-radius:12px}.anomaly-warning{background:var(--amber-soft)}.anomaly-info{background:var(--brand-soft)}.anomaly-icon{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;flex:none;font-size:14px}.anomaly-warning .anomaly-icon{background:var(--amber);color:#20170a}.anomaly-info .anomaly-icon{background:var(--brand);color:#fff}.anomaly-card strong{display:block;font-size:12px;margin-bottom:2px}.anomaly-card p{margin:0;font-size:11px}
.forecast-card{padding:12px;background:var(--surface-2);border-radius:12px}.forecast-range{text-align:center;margin-bottom:8px}.forecast-range strong{display:block;font-size:20px}.forecast-range span{color:var(--muted);font-size:11px}.forecast-meta{color:var(--muted);font-size:10px;text-align:center}
.roadmap-list{display:grid;gap:4px}.roadmap-row{display:grid;grid-template-columns:160px 1fr 100px;gap:12px;align-items:center;min-height:34px;padding:6px 0;border-top:1px solid var(--line);font-size:11px}.roadmap-row:first-child{border-top:0}.roadmap-cap{font-weight:600}.roadmap-req{color:var(--muted)}.roadmap-status{color:var(--amber);font-weight:600;text-align:right}
"""

marker = '.dashboard-error button{margin-left:10px;background:var(--risk)}'
media_marker = '@media(max-width:1050px)'

# Find the injection point
idx = content.find(marker)
if idx == -1:
    print("ERROR: marker not found!")
    sys.exit(1)

end_marker = idx + len(marker)
after_marker = content[end_marker:]

# The content between marker and first media query should have our styles
# Remove any existing home styles that might be there
# Insert new styles
new_content = content[:end_marker] + home_styles + after_marker

with open(css_path, 'w', encoding='utf-8') as f:
    f.write(new_content)

# Verify the injection
with open(css_path, 'r', encoding='utf-8') as f:
    verify = f.read()
    
checks = [
    '.headline-block{display:flex',
    '.home-metrics-grid{display:grid',
    '.pulse-header{display:grid',
    '.alert-row{display:grid',
    '.cohort-home-stats{display:grid',
    '.sources-home-grid{display:grid',
    '.home-link{color:var(--brand)',
    '.anomaly-card{display:flex',
    '.forecast-card{padding:12px',
    '.roadmap-list{display:grid',
]

all_ok = True
for check_str in checks:
    if check_str in verify:
        print(f"  OK: {check_str[:50]}...")
    else:
        print(f"  MISSING: {check_str[:50]}...")
        all_ok = False

if all_ok:
    print(f"\nAll home styles verified. File size: {len(verify)} bytes")
else:
    print(f"\nSome styles are missing! File size: {len(verify)} bytes")
