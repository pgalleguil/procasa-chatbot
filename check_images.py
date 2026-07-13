"""Debug script to directly test _extract_with_bs4 on the cached HTML."""
import sys
sys.path.insert(0, 'scraper')

import hashlib, pathlib

url = 'https://www.yapo.cl/bienes-raices-venta-de-propiedades-casas/casa-en-venta-en-la-florida/32464287'
fid = hashlib.md5(url.encode()).hexdigest()
p = pathlib.Path('scraper/html_dumps') / (fid + '.html')
print('HTML path:', p, '| exists:', p.exists())

if p.exists():
    html = p.read_text(encoding='utf-8', errors='replace')
    from scraping_yapo_proxys import _extract_with_bs4, BeautifulSoup
    print('BeautifulSoup available:', BeautifulSoup is not None)
    result = _extract_with_bs4(html)
    imgs = result.get('image_urls', result.get('images', []))
    print('image_urls_count:', len(imgs))
    print('visual_image_source:', result.get('visual_image_source'))
    for i, u in enumerate(imgs[:5]):
        print(f'  [{i+1}] {u}')
