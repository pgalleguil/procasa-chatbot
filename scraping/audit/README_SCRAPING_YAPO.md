# README_SCRAPING_YAPO.md

## LEER ANTES DE MODIFICAR

Esta carpeta contiene los procesos principales de scraping y mantenimiento de propiedades Yapo.

Antes de eliminar archivos o modificar lógica, revisar este documento.

---

# ARCHIVOS PRINCIPALES

## scraping_yapo_proxys.py

### Qué hace

Scraper principal del sistema.

Responsable de:

* Descubrir propiedades nuevas.
* Descargar HTML.
* Guardar HTML en html_dumps/.
* Extraer datos de la propiedad.
* Detectar dueños y corredoras.
* Guardar información en MongoDB.

### Ejecutar

```bash
python scraping_yapo_proxys.py
```

### Se puede eliminar

❌ NO

---

## validate_properties.py

### Qué hace

Verifica si las propiedades siguen publicadas en Yapo.

Actualiza:

* status
* last_verified
* inactive_date
* failed_checks

### Optimización implementada

Las propiedades clasificadas como corredora NO se validan.

Esto reduce el consumo de proxies.

### Ejecutar

```bash
python validate_properties.py
```

### Prueba

```bash
python validate_properties.py --dry-run
```

### Se puede eliminar

❌ NO

---

## update_history_brokers.py

### Qué hace

Reprocesa propiedades históricas utilizando HTML guardados localmente.

NO realiza scraping.

NO consume proxies.

NO descarga páginas.

Lee:

```text
html_dumps/
```

y actualiza MongoDB.

### Cuándo usarlo

Cuando se descubra una nueva señal en el HTML.

Ejemplos:

* broker_brand
* contact_logo
* seller_profile_id
* data-company-id
* futuros patrones

### Ejecutar

```bash
python update_history_brokers.py
```

### Se puede eliminar

⚠️ NO RECOMENDADO

Mover a:

```text
scripts/maintenance/
```

---

# HTML HISTÓRICOS

## IMPORTANTE

NO ELIMINAR:

```text
html_dumps/
```

Estos archivos permiten:

* Corregir errores históricos.
* Descubrir nuevas señales.
* Actualizar MongoDB.
* Evitar consumo de proxies.

La migración de junio 2026 fue posible gracias a estos archivos.

---

# MIGRACIÓN JUNIO 2026

## Problema detectado

Muchas corredoras publicaban usando nombres de personas.

Ejemplo:

Nombre visible:

```text
Jessica Krauss
```

Pero el HTML contenía:

```html
<img alt="Kutt Property">
```

El scraper antiguo no detectaba esto.

---

## Solución

Se agregó detección de:

* broker_brand
* contact_logo img alt
* seller_profile_id

---

## Resultado

Propiedades inicialmente clasificadas como dueño:

```text
1292
```

Con HTML disponible:

```text
1273
```

Reclasificadas como corredora:

```text
869
```

Errores:

```text
0
```

---

# VERSIONADO

Se agregaron:

```json
{
  "html_version": 1,
  "parsed_version": 3
}
```

### Para qué sirve

Permite saber qué registros fueron procesados con qué versión de extracción.

Si se descubre una nueva señal:

1. Modificar update_history_brokers.py
2. Incrementar parsed_version
3. Reprocesar HTML históricos

Sin usar proxies.

---

# ARCHIVOS QUE SE PUEDEN ELIMINAR SI APARECEN

Normalmente son temporales:

```text
check_*.py
debug_*.py
test_*.py
tmp_*.py
verify_*.py
progress.py
migration.log
```

Solo verificar antes que no estén siendo usados.


# ARCHIVOS QUE SOLO ELIMINA LOS TELEFONOS PORQUE HUBO PROBLEMA CON EXTRACCIÓN DE TELEFONO EL EN ARCHVO PHONE_CONTACT_EXTRACTOR

phones_contact_extractor.py

delete_phones.py

---

# REGLA DE ORO

Si existe HTML histórico:

❌ No volver a scrapear.

✅ Reprocesar HTML existente.

Siempre será más rápido, más barato y consumirá menos proxies.

---

Última actualización:

Junio 2026

Motivo:

Detección masiva de corredoras ocultas mediante HTML históricos.
