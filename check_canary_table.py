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

print(f"{'listing_id':<10} | {'schema':<20} | {'url':<25} | {'final_state':<15} | {'seller_name':<20} | {'operacion':<10} | {'tipo':<15} | {'comuna':<15} | {'region':<15} | {'fecha':<10} | {'dorm':<5} | {'ban':<4} | {'est':<4} | {'precio_raw':<15} | {'precio_uf':<10} | {'precio_clp':<15} | {'validacion':<15} | {'img_cnt':<7} | {'body_text':<10}")
print("-" * 250)

for x in d.get('results', [])[:5]:
    l_id = str(x.get('listing_id'))
    schema = str(x.get('schema_version'))
    url = str(x.get('url'))[:25]
    fstate = str(x.get('classification', {}).get('final_state'))
    sname = str(x.get('seller_name'))[:20]
    op = str(x.get('operacion'))
    tipo = str(x.get('tipo_propiedad'))
    com = str(x.get('comuna'))
    reg = str(x.get('region'))
    fecha = str(x.get('fecha_publicacion'))
    dorm = str(x.get('dormitorios'))
    ban = str(x.get('banos'))
    est = str(x.get('estacionamientos'))
    praw = str(x.get('precio_raw'))
    puf = str(x.get('precio_uf'))
    pclp = str(x.get('precio_clp'))
    pval = str(x.get('precio_validacion'))
    icnt = str(x.get('image_urls_count'))
    body = "True" if "body_text_len" in x and "body_text" not in x else "False"
    
    print(f"{l_id:<10} | {schema:<20} | {url:<25} | {fstate:<15} | {sname:<20} | {op:<10} | {tipo:<15} | {com:<15} | {reg:<15} | {fecha:<10} | {dorm:<5} | {ban:<4} | {est:<4} | {praw:<15} | {puf:<10} | {pclp:<15} | {pval:<15} | {icnt:<7} | {body:<10}")

