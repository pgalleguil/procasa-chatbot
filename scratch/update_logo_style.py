import glob

files = [
    'templates/captacion_detail.html',
    'templates/captacion_list.html',
    'templates/crm_leads_list.html',
    'templates/crm_lead_detail.html',
    'templates/leads_dashboard.html',
    'templates/manual_lead_entry.html',
    'templates/contract_dashboard.html',
    'templates/visita_dashboard.html'
]

# We want to replace the brand img inline style across all these templates
# to have a fixed, non-scaling width and left alignment.
old_style_patterns = [
    'style="max-height: 55px; width: auto; min-width: var(--sidebar-width-collapsed); object-fit: contain; padding: 0 15px;"',
    'style="max-height: 30px; width: auto; min-width: var(--sidebar-width-collapsed); object-fit: contain; padding: 0 15px;"',
    'style="\n            max-height: 30px;\n            width: auto;\n            min-width: var(--sidebar-width-collapsed);\n            object-fit: contain;\n            padding: 0 15px;\n            "'
]

target_style = 'style="max-height: 55px; width: 140px; min-width: 140px; object-fit: contain; object-position: left; padding: 0; margin-left: 15px;"'

for fn in files:
    print(f"Updating logo in {fn}...")
    with open(fn, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    new_content = content
    for pattern in old_style_patterns:
        new_content = new_content.replace(pattern, target_style)

    # In case there's slightly different formatting, let's do a regex replacement as fallback
    import re
    # Match any style inside img that contains logo.png
    # Specifically replacing style=\"...\" where the image tag has src=\"/static/logo.png...\"
    def replacer(match):
        img_tag = match.group(0)
        # Replace the style attribute inside this tag
        updated_tag = re.sub(r'style="[^"]+"', target_style, img_tag)
        # Handle multiline style just in case
        updated_tag = re.sub(r'style="\s*[^"]+\s*"', target_style, updated_tag)
        return updated_tag

    new_content = re.sub(r'<img[^>]*src="/static/logo.png[^>]*>', replacer, new_content)

    if new_content != content:
        print(f"  Successfully updated logo styling in {fn}")
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(new_content)
    else:
        print(f"  No change or already updated in {fn}")

print("Done standardizing all logo styles!")
