import json
import glob
import os

files = glob.glob('scraper/reports/yapo_inciertos_*.json')
if not files:
    print('No reports found')
    exit()

latest_file = max(files, key=os.path.getctime)
print(f'Reading {latest_file}')

with open(latest_file, 'r', encoding='utf-8') as f:
    d = json.load(f)

for x in d.get('results', [])[:5]:
    print('---')
    print(f"listing_id: {x.get('listing_id')}")
    print(f"url: {x.get('url')}")
    print(f"seller_name: {x.get('seller_name')}")
    print(f"final_state: {x.get('classification', {}).get('final_state')}")
    print(f"operacion: {x.get('operacion')}")
    print(f"tipo_propiedad: {x.get('tipo_propiedad')}")
    print(f"comuna: {x.get('comuna')}")
    print(f"fecha_publicacion: {x.get('fecha_publicacion')}")
    print(f"dormitorios: {x.get('dormitorios')}")
    print(f"banos: {x.get('banos')}")
    print(f"estacionamientos: {x.get('estacionamientos')}")
    print(f"precio_raw: {x.get('precio_raw')}")
    print(f"precio_moneda_original: {x.get('precio_moneda_original')}")
    print(f"precio_original_num: {x.get('precio_original_num')}")
    print(f"precio_uf: {x.get('precio_uf')}")
    print(f"precio_clp: {x.get('precio_clp')}")
    print(f"precio_validacion: {x.get('precio_validacion')}")
    print(f"image_urls_count: {x.get('image_urls_count')}")
    print(f"main_image_url: {x.get('main_image_url')}")
