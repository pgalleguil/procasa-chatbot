# Auditoría V3 Yapo — Maipú, 482 anuncios

Generada el 2026-11-04T13:51:05.363471+00:00. Es una reclasificación local sobre el JSON aprobado; no repite scraping.

## Resultado

- Estados propuestos: {"CORREDOR_PROBABLE": 44, "CORREDOR_SEGURO": 369, "DUEÑO_PROBABLE": 11, "INCIERTO": 58}.
- Cambios de estado frente al lote: 34.
- Corredores adicionales frente al lote inicial: 33.
- Dueños probables agregados: 1.
- INCIERTO restantes: 58.
- Señales débiles para priorizar revisión de dueño: 43 anuncios; estas señales no cambian el estado por sí solas.

## Perfiles

- 183 `seller_profile_id` distintos en los 482 registros.
- 20 perfiles tienen 3 o más anuncios distintos observados dentro del lote local.
- Es un mínimo del lote, no el total del perfil Yapo. Páginas en vivo: 0; enumeraciones completas: False.
- El scraper general integra correlación por ID exacto y limita el rastreo futuro a perfiles únicos, páginas acotadas y FlashProxy explícito.

## Disponibilidad

- Anuncios reportados desactivados por el usuario: 4.
- Confirmados activos: 0; confirmados desactivados: 0; sin verificar: 4.
- No había `FLASHPROXY_PROXY_URL` ni aplicación FlashProxy disponible; por eso no se hicieron solicitudes. El HTML guardado no prueba el estado actual.
- El código no infiere ACTIVO por HTTP 200. Los no verificados y desactivados quedan con bloqueo de revisión en el flujo Yapo.

## Observaciones manuales y regla corregida

Se conservaron las 21 observaciones, sus textos y descripciones completas en `manual_audit_21.csv`; son opiniones humanas, no etiquetas de verdad.

Una observación detectó un defecto reproducible: el ID **28073704** menciona “2% de honorarios de Corretaje” y quedaba INCIERTO. Ahora se propone CORREDOR_PROBABLE, con atribución de comisión `unattributed_charge`, evidencia media y revisión requerida; no se promueve a corredor seguro. Las comisiones explícitamente cobradas por terceros no clasifican al publicador como corredor.

Las señales de descripción breve, pocas fotos, primera persona, trato directo y sin comisión solo agregan prioridad de revisión. No se inventan probabilidades ni se promueve un dueño por ausencia de evidencia comercial.

## Integridad y límites

- SHA-256 del JSON original: `dc9bda51a093abd9a1d7c6a741eeacac275724ea74b29ad4cc2a8bba558e12bf`; verificado nuevamente: `dc9bda51a093abd9a1d7c6a741eeacac275724ea74b29ad4cc2a8bba558e12bf`.
- El JSON canónico no se modificó. El CSV contiene los 482 `listing_id` únicos y la comparación antes/después.
- No se consultó MongoDB, DeepSeek ni Yapo; no hubo importación, distribución, asignación ni notificaciones.
- No se afirma una tasa de error del clasificador: las observaciones manuales no son un conjunto de verdad etiquetado.
- La suite previa fue 363 aprobadas y 2 omitidas; la regresión completa final fue 380 aprobadas y 2 omitidas, incluidas 17 pruebas V3.

## Archivos

- `classification_comparison_482.csv`: una fila por anuncio, con evidencia, contradicciones y señales de revisión.
- `manual_audit_21.csv`: las 21 observaciones, descripción completa sin recortar y contraste con HTML local.
- `publisher_profile_audit.csv`: perfiles exactos, anuncios del lote, comunas, operaciones, tipos y evidencias observadas; todos marcados incompletos/no verificados en el sitio.
- `availability_audit.csv`: los cuatro anuncios reportados como desactivados y su estado actual NO_VERIFICADO.
- `quality_summary.json`: conteos, integridad y límites de la auditoría.
