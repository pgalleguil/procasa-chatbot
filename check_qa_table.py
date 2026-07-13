import json
import glob
import os
import re

files = glob.glob('scraper/reports/yapo_inciertos_*.json')
if not files:
    print('No reports found')
    exit()

latest_file = max(files, key=os.path.getctime)
print(f'Reading {latest_file}\n')

with open(latest_file, 'r', encoding='utf-8') as f:
    d = json.load(f)

for x in d.get('results', [])[:5]:
    cls = x.get('classification', {})
    raw_attrs = x.get('raw_attributes', {})
    image_urls = x.get('image_urls', [])
    
    # Check for f_auto/f_auto in any url
    faut_dupe = any('f_auto/f_auto' in u for u in image_urls)
    
    # Get localizacion from raw_attrs
    loc_val = raw_attrs.get('localización') or raw_attrs.get('localizacion') or raw_attrs.get('ubicación') or raw_attrs.get('ubicacion') or ''
    
    body_text_full_stored = 'body_text' in x
    
    print(f"listing_id         : {x.get('listing_id')}")
    print(f"schema_version     : {x.get('schema_version')}")
    print(f"seller_name        : {x.get('seller_name')!r}")
    print(f"final_state        : {cls.get('final_state')}")
    print(f"comuna             : {x.get('comuna')!r}")
    print(f"raw_attrs.local.   : {loc_val!r}")
    print(f"operacion          : {x.get('operacion')}")
    print(f"tipo_propiedad     : {x.get('tipo_propiedad')}")
    print(f"dormitorios        : {x.get('dormitorios')}")
    print(f"banos              : {x.get('banos')}")
    print(f"estacionamientos   : {x.get('estacionamientos')}")
    print(f"precio_raw         : {x.get('precio_raw')}")
    print(f"precio_uf          : {x.get('precio_uf')}")
    print(f"precio_clp         : {x.get('precio_clp')}")
    print(f"image_urls_count   : {x.get('image_urls_count')} (len={len(image_urls)})")
    print(f"img_detected_count : {x.get('image_urls_detected_count')}")
    print(f"main_image_url     : {x.get('main_image_url', '')[:70]}")
    print(f"f_auto/f_auto BUG  : {'YES [BUG]' if faut_dupe else 'no [OK]'}")
    print(f"body_text stored   : {'YES [BAD]' if body_text_full_stored else 'no [OK]'}")
    print(f"raw_attributes_keys: {list(raw_attrs.keys())}")
    print("-" * 60)
