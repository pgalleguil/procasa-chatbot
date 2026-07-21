"""Replace home view CSS with compact new layout."""
css_path = r'C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\static\css\analytics-dashboard.css'

with open(css_path, 'r', encoding='utf-8') as f:
    content = f.read()

new_home_styles = """
.home-priority{padding:20px 22px}.priority-main{display:flex;align-items:flex-start;gap:16px}.priority-main-icon{width:38px;height:38px;border-radius:50%;display:grid;place-items:center;flex:none;font-size:16px;color:#fff}.priority-main-content{flex:1;min-width:0}.priority-severity{font-size:9px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;margin-bottom:2px}.priority-main-critical .priority-severity{color:var(--risk)}.priority-main-warning .priority-severity{color:var(--amber)}.priority-main-positive .priority-severity{color:var(--ok)}.priority-main-title{font-size:16px;font-weight:700;line-height:1.25;margin-bottom:3px;display:block}.priority-main-detail{font-size:11px;color:var(--muted);margin:0}.priority-main-action{flex:none;padding:7px 14px;background:var(--brand-soft);color:var(--brand);border-radius:8px;text-decoration:none;font-weight:600;font-size:11px;display:inline-flex;align-items:center;gap:5px;white-space:nowrap}.priority-main-action:hover{background:var(--brand);color:#fff}.priority-secondary{margin-top:10px;padding-top:10px;border-top:1px solid var(--line);display:grid;gap:6px}.priority-secondary-row{display:grid;grid-template-columns:16px 34px 1fr;align-items:center;gap:8px;font-size:11px;min-height:28px}.priority-secondary-row .sec-icon{font-size:11px;text-align:center}.priority-secondary-row .sec-count{font-weight:700;font-size:14px}.priority-secondary-row .sec-label{color:var(--muted)}
.home-grid-2col{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.pulse-body{display:grid;gap:0}.pulse-row{display:grid;grid-template-columns:minmax(90px,1fr) 56px 64px 72px 64px;gap:4px;align-items:center;min-height:34px;border-bottom:1px solid var(--line);font-size:11px;padding:5px 0}.pulse-row:last-child{border-bottom:0}.pulse-row.header{color:var(--muted);font-size:9px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;border-bottom:1px solid var(--line);min-height:26px;padding:4px 0}.pulse-label{font-weight:600;font-size:11px}.pulse-val{font-weight:700;font-size:14px;color:var(--text)}.pulse-vs{color:var(--muted);font-size:10px}.pulse-pct{font-weight:700;font-size:11px}.pulse-pct.up{color:var(--ok)}.pulse-pct.down{color:var(--risk)}.pulse-hotcold{display:flex;gap:0;margin-top:8px;border-radius:6px;overflow:hidden;height:6px}.pulse-hotcold .hot{background:var(--risk);height:100%}.pulse-hotcold .cold{background:var(--cyan);height:100%}.pulse-hotcold .unknown{background:var(--muted);height:100%}.pulse-labels{display:flex;gap:14px;margin-top:5px;font-size:9px;color:var(--muted)}.pulse-labels span{display:flex;align-items:center;gap:4px}.pulse-labels i{width:6px;height:6px;border-radius:2px;display:inline-block}
.management-card{text-align:center;padding:12px 0}.management-status{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;background:var(--amber-soft);border-radius:8px;margin-bottom:10px;font-size:11px;font-weight:600;color:var(--amber)}.management-card p{font-size:11px;color:var(--muted);margin:4px 0}.management-coverage{display:flex;justify-content:center;gap:20px;margin-top:10px}.management-stat{text-align:center}.management-stat strong{display:block;font-size:18px;font-weight:700}.management-stat span{font-size:9px;color:var(--muted);display:block;margin-top:1px}
.signal-card{display:flex;align-items:flex-start;gap:12px;padding:8px 0}.signal-icon{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;flex:none;font-size:13px}.signal-warning .signal-icon{background:var(--amber);color:#20170a}.signal-info .signal-icon{background:var(--brand);color:#fff}.signal-content{flex:1}.signal-content strong{display:block;font-size:12px;margin-bottom:2px}.signal-content p{font-size:11px;margin:0}
"""

marker = '.dashboard-error button{margin-left:10px;background:var(--risk)}'
media_marker = '@media(max-width:1050px)'

idx = content.find(marker)
if idx == -1:
    print("ERROR: marker not found!")
    import sys; sys.exit(1)

end_marker = idx + len(marker)
after_marker = content[end_marker:]

# Remove existing home styles (between marker and media query)
# Find where the media query starts
mq_idx = after_marker.find(media_marker)
if mq_idx == -1:
    print("ERROR: media query not found!")
    import sys; sys.exit(1)

between = after_marker[:mq_idx]
rest = after_marker[mq_idx:]

# Check if there are old home styles to remove
old_home_start = between.find('.home-headline')
old_home_end = between.rfind('}')
if old_home_start != -1:
    # Remove old home styles
    between_cleaned = between[:old_home_start] + between[old_home_end+1:]
else:
    between_cleaned = between

new_content = content[:end_marker] + between_cleaned + new_home_styles + rest

with open(css_path, 'w', encoding='utf-8') as f:
    f.write(new_content)

# Verify
with open(css_path, 'r', encoding='utf-8') as f:
    verify = f.read()

checks = [
    '.home-priority{padding:20px 22px}',
    '.priority-main{display:flex',
    '.home-grid-2col{display:grid',
    '.pulse-row{display:grid',
    '.management-card{text-align:center',
    '.signal-card{display:flex',
]
all_ok = True
for check_str in checks:
    if check_str in verify:
        print(f"  OK: {check_str}")
    else:
        print(f"  MISSING: {check_str}")
        all_ok = False

print(f"\n{'All styles OK' if all_ok else 'SOME STYLES MISSING'}. File size: {len(verify)} bytes")
