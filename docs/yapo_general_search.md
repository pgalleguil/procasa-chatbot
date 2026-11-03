# Motor general de búsqueda Yapo

El punto de entrada es `python -m scrapers.scraper_yapo.ejecutar_busqueda`. La simulación offline es el modo predeterminado. `--live` habilita consultas Yapo exclusivamente mediante FlashProxy y deduplicación MongoDB de solo lectura. El motor no escribe en MongoDB, no distribuye, no asigna y no invoca DeepSeek.

Para convertir CLP a UF, entrega conjuntamente `--uf-valor-clp`, `--uf-fecha` y `--uf-fuente`. La prueba del 3 de noviembre de 2026 usa 41.263,57 CLP por UF según la tabla diaria del [SII](https://www.sii.cl/valores_y_fechas/uf/uf2026.htm); si la fecha de la tasa no está dentro de los 7 días, el precio convertido queda pendiente.

## Filtros y estado de verificación

| Filtro | Estado | Comportamiento |
| --- | --- | --- |
| Región + una comuna | Ruta observada en HTML guardado; verificación canónica en cada página real | Exige ruta exacta y valida la comuna extraída en cada ficha. |
| Varias comunas | Implementado como una búsqueda separada por comuna | Deduplica anuncios repetidos antes de consultar MongoDB. La corrida queda incompleta si falla cualquiera. |
| Todas las comunas de una región | No soportado | La CLI se detiene porque no hay un índice completo de comunas Yapo verificado. |
| Venta y arriendo | Rutas de categorías observadas en el HTML guardado | La ruta de cada ficha se vuelve a verificar contra la operación y el tipo pedidos. |
| Casa, departamento y otros tipos registrados | Rutas candidatas tomadas de HTML y fichas guardadas | La página canónica de Yapo debe conservar la ruta exacta antes de extraer anuncios. Algunas combinaciones no existen y se rechazan. |
| Precio UF | `f_currency.CLF` y rangos observados en URLs canónicas guardadas | Un valor CLP solo se compara con UF si la tasa oficial indicada tiene fecha dentro de los últimos 7 días. |
| Precio CLP | El token `f_currency.CLP` se prueba en la página canónica antes de procesar anuncios | Aún no se verificó en esta corrida real. Si Yapo descarta o cambia el filtro, la búsqueda queda bloqueada. |
| Precio mínimo sin máximo | Sintaxis abierta `f_price.<mínimo>-` observada en URLs guardadas | El filtro sigue sujeto a verificación canónica durante la corrida. |
| Precio máximo sin mínimo | No verificado | Se bloquea antes de consultar Yapo. |
| Dormitorios | `f_rooms` observado en HTML y una consulta `5-5` guardada | La URL canónica de Pudahuel conservó `f_rooms.3-`. La ficha descargada no entregó dormitorios, por lo que el anuncio quedó pendiente y no cuenta como validación comercial del dato. |
| Baños y superficie | Solo se observó el campo de interfaz; no su token de consulta | La configuración se bloquea hasta verificar la codificación de URL. |

`--max-pages` deja la paginación incompleta si se alcanza el límite mientras Yapo aún ofrece una página siguiente. Si Yapo confirma el final real antes del límite, `pagination_complete` queda verdadero. `--max-listings` es independiente: el paginador sigue avanzando, pero `listing_selection_complete` queda falso si se conservan solo algunos anuncios. Los errores de proxy o las páginas que pierden filtros siempre dejan la corrida incompleta.

## Comandos

Maipú, venta de casa, 3.000–6.000 UF (offline):

```powershell
python -m scrapers.scraper_yapo.ejecutar_busqueda --region "Región Metropolitana" --comuna Maipú --operacion venta --tipo casa --precio-min 3000 --precio-max 6000 --moneda UF --batch-id maipu_3000_6000_offline
```

Pudahuel, venta de departamento, 2.000–4.000 UF, mínimo 3 dormitorios. La prueba real por FlashProxy verificó las dos páginas de Yapo y sus filtros canónicos. Se conservaron 1 de 40 anuncios por `--max-listings 1` (selección truncada); el registro quedó pendiente porque la ficha no expuso dormitorios. La corrida usó 10 solicitudes de proxy, reutilizó la página 1 y la ficha desde caché, y no escribió en MongoDB ni llamó DeepSeek. Archivos: `C:\Users\pgall\Documents\PROCASA\YapoMaipu\general_search_20261103\yapo_search_p0_pudahuel_page2_probe_20261103_summary.json`, `..._records.json` y `..._records.csv`.

```powershell
python -m scrapers.scraper_yapo.ejecutar_busqueda --region "Región Metropolitana" --comuna Pudahuel --operacion venta --tipo departamento --precio-min 2000 --precio-max 4000 --moneda UF --dormitorios-min 3 --max-pages 2 --max-listings 1 --batch-id pudahuel_departamento_probe --live
```

La Florida, arriendo de departamento, rango CLP (comando de simulación; al habilitar `--live`, Yapo debe confirmar los tokens CLP en su URL canónica):

```powershell
python -m scrapers.scraper_yapo.ejecutar_busqueda --region "Región Metropolitana" --comuna "La Florida" --operacion arriendo --tipo departamento --precio-min 500000 --precio-max 1200000 --moneda CLP --batch-id la_florida_arriendo_clp
```

Dos comunas en una ejecución:

```powershell
python -m scrapers.scraper_yapo.ejecutar_busqueda --region "Región Metropolitana" --comuna Maipú --comuna Pudahuel --operacion venta --tipo casa --precio-min 3000 --precio-max 6000 --moneda UF --batch-id maipu_pudahuel_casas
```

Sin filtro de precio, omite `--precio-min`, `--precio-max` y `--moneda`. La sintaxis de máximo abierto se rechaza mientras no exista una verificación de Yapo.
