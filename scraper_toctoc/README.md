# Scraper Toctoc.com

## Arquitectura

Basada en la misma arquitectura modular de `scraper/` (la misma que usa Yapo):

```
scraper_toctoc/
├── config.py              # Config desde .env
├── discovery.py           # Descubrimiento de URLs via SSR
├── downloader.py          # HTTP + Playwright fallback
├── extractor.py           # Parseo con selectores reales de toctoc
├── classifier_rules.py    # Clasificacion rule-based (brands, terms)
├── deepseek_classifier.py # Clasificacion con DeepSeek
├── enrich.py              # Enriquecimiento (UF, tipo, comuna)
├── mongo_store.py         # MongoDB upsert con $set
├── proxy_manager.py       # Rotacion de proxies
├── run_toctoc.py          # CLI orchestrator
├── data/                  # Reglas JSON
├── html_dumps/            # HTML crudo por MD5(url)
└── reports/               # JSON intermediate
```

## Como funciona

### Stage 1: Discovery
- Usa URLs SSR de Next.js: `/venta/{tipo}/{region}/{comuna}`
- El HTML contiene `__NEXT_DATA__` con 20 propiedades en `pageProps.propiedades.results`
- Cada propiedad tiene `urlFicha` (URL de detalle completa)
- Total: 1061 propiedades para La Florida, pero SSR solo da page 1 (20)
- **Paginacion**: No existe en SSR (?pagina=X devuelve siempre page 1). Para obtener mas hay que:
  - Opcion A: Varias busquedas (distintas comunas, tipos, operaciones)
  - Opcion B: Usar Playwright para hacer clic en `<a class="page-link" aria-label="Next">›</a>`

### Stage 2: Download
- Intenta primero HTTP (requests)
- Si falla (INVALID/BLOCKED), hace fallback a Playwright
- Playwright usa `wait_until="domcontentloaded"` + 5s wait (evita timeout de `networkidle`)
- Guarda HTML en `html_dumps/{batch_id}/{md5(url)}.html`
- Validacion: `LISTING_REMOVED` si hay patrones de anuncio borrado; `BLOCKED` si hay patrones de bloqueo en texto visible (body, no scripts)

### Stage 3: Parse
- Si el HTML tiene `__NEXT_DATA__` (SSR), extrae titulo, precio, fotos, publicador desde ahi
- Si no (SPA renderizada con Playwright), usa BeautifulSoup con selectores reales:
  - `h1.tipo.nv` → titulo y operacion (venta/arriendo del <strong>)
  - `p.text-justify.texto` → descripcion
  - `p.precio-uf` → precio UF
  - `p.precio-alt` → precio CLP
  - `div.cf-contacto .info-anunciante` → publicador (nombre, logo, tipo)
  - `img[alt="img galería"]` → imagenes
- Comuna y region se extraen de la URL

### Stage 4: Clasificacion
Igual que Yapo:
1. **Reglas**: Busca marcas conocidas y terminos de corredor en campos fuertes
2. **Si INCONCLUSIVE**: Llama a DeepSeek con nombre del publicador + descripcion
3. Estados: `CORREDOR_SEGURO`, `CORREDOR_PROBABLE`, `DUEÑO_SEGURO`, `INCIERTO`, `AD_REMOVED`

### Stage 5: MongoDB
- Misma coleccion `toctoc_properties` en DB `yapo`
- Misma operacion: `$set` (nunca `$unset`)
- Mismos campos: url, listing_id, title, price_uf, price_clp, comuna, region, operacion, tipo_propiedad, publicador_visible, seller_type, images, descripcion, classification, etc.

## Comandos

```bash
# Descubrir (SSR da max 20)
python run_toctoc.py discover --comuna "la-florida" --max-urls 20

# Procesar detalles (usa Playwright)
python run_toctoc.py process --batch-id <id> --limit 5 --use-playwright --no-llm

# Full pipeline
python run_toctoc.py run-full --comuna "la-florida" --max-urls 5 --limit 5 --use-playwright --no-llm
```

## Desafios pendientes

### 1. Paginacion
La SSR de Next.js siempre devuelve page 1 (20 propiedades). El total para La Florida es ~1061 propiedades. Para obtener las demas:
- **Corto plazo**: Varias busquedas con distintas comunas/tipos/operaciones
- **Largo plazo**: Usar Playwright en discovery para hacer clic en Next y extraer mas URLs desde el DOM renderizado (class="page-link" aria-label="Next")

### 2. Extraer mas campos del detalle
Faltan algunos campos del SSR que no estan en los selectors actuales:
- `dormitorios` (viene en `propiedades.results[].dormitorios` del SSR)
- `banos` (viene en `propiedades.results[].bannos` del SSR)
- `superficie` (viene en `propiedades.results[].superficie` del SSR)
- `region` (extraerla de la URL correctamente)

### 3. Selectores para propiedades usadas (no nuevas)
Actualmente probado solo con `/propiedades/compranuevo/...`. Las propiedades usadas tienen otra URL: `/propiedad/{slug}-{id}`. Habria que verificar si los selectores cambian.

### 4. Anti-bot
Playwright funciona pero puede ser detectado. Si toctoc refuerza su proteccion:
- Rotar user-agents
- Usar proxies
- Modificar fingerprint de Playwright (viewport, locale, timezone)

### 5. Seller type
Detectar si el publicador es "particular" (dueño) o "empresa" (corredora/inmobiliaria):
- El texto "Anunciante - Particular" en `.info-anunciante li.titulo strong`
- Vs "Anunciante" sin "Particular" mas logo de empresa
- Esto alimenta directamente la clasificacion

### 6. Clasificacion con DeepSeek
Cuando hay DeepSeek habilitado, analiza el publicador + descripcion. Habria que verificar que los prompts sean adecuados para toctoc (no menciones "Yapo").

### 7. region desde la URL
La URL SSR es `/venta/departamento/metropolitana/la-florida`. La URL de detalle es `/propiedades/compranuevo/departamento/la-florida/edificio-refugio-new/1384492`. La region "metropolitana" no esta en la URL de detalle. Opciones:
- Pasarla desde el discovery (el SSR si tiene la region)
- Extraerla del `__NEXT_DATA__` del SSR
- Buscar en la pagina de detalle algun elemento con la region

### 8. Pruebas con arriendo
Solo probado con operacion=venta. Habria que verificar que `/arriendo/...` tambien funciona.
