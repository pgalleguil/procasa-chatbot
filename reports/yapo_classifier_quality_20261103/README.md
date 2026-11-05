# Auditoría de calidad del clasificador Yapo — 2026-11-03

## Alcance y preservación

Se revisó el lote canónico de 482 anuncios desde `C:\Users\pgall\Documents\PROCASA\YapoMaipu\preimportacion_20261103\yapo_maipu_preimport_482.json`. Su SHA-256 esperado y verificado antes y después es `dc9bda51a093abd9a1d7c6a741eeacac275724ea74b29ad4cc2a8bba558e12bf`. El JSON canónico no se modificó.

Se identificaron exactamente 92 anuncios `INCIERTO`. La evaluación usó esos 482 registros y HTML local. No hubo solicitudes a Yapo, escrituras MongoDB, importaciones, asignaciones, distribuciones, notificaciones ni llamadas a DeepSeek. No se verificaron identidades externamente; nombres parecidos no se trataron como identidad confirmada.

## Muestra aleatoria reproducible de 20

Selección: `random.Random(20261103).sample(uncertain_records_in_canonical_order, 20)`.

- 1. `32299493`
- 2. `33098407`
- 3. `33196456`
- 4. `32931491`
- 5. `26143551`
- 6. `25939035`
- 7. `33190907`
- 8. `26610549`
- 9. `33208287`
- 10. `28073704`
- 11. `32632678`
- 12. `27167566`
- 13. `27144918`
- 14. `32306422`
- 15. `32728499`
- 16. `25861896`
- 17. `26135394`
- 18. `26644524`
- 19. `26653392`
- 20. `27111189`

`audit_sample_20.json` contiene el título y descripción completa, campos de identidad y perfil, anuncios locales del mismo `seller_profile_id`, extracción del HTML, evidencias, señales de propiedad, contradicciones y conclusión por anuncio.

## Integridad de los datos locales

- HTML local existente: 92/92.
- SHA-256 HTML coincidente con el lote: 92/92.
- Título y descripción completa coincidentes con el HTML: 92/92.
- Tipo de anunciante reextraído del HTML coincidente: 92/92.
- Descripciones marcadas como truncadas al reextraer: 0.

## Comparación de los 92

| Estado | Antes | Después |
|---|---:|---:|
| `INCIERTO` | 92 | 59 |
| `CORREDOR_SEGURO` | 0 | 0 |
| `CORREDOR_PROBABLE` | 0 | 32 |
| `DUEÑO_PROBABLE` | 0 | 1 |

Cambios deterministas respaldados: 33. Los corredores quedan como propuestas `CORREDOR_PROBABLE`; ninguno se promovió a `CORREDOR_SEGURO`. El dueño queda como `DUEÑO_PROBABLE`, sin afirmar titularidad legal. No se calcula tasa de error: la muestra no es un conjunto de etiquetas verificadas.

El CSV `comparison_92.csv` contiene ID, publicador, perfil, estados antes/después, evidencia, señales de dueño, contradicciones, avisos del mismo perfil, estado de confirmación e integridad HTML.

## Defectos demostrados y cambios puntuales

1. El extractor guardaba el atributo de aviso `Agente` pero el clasificador lo ignoraba. Ahora es evidencia para `CORREDOR_PROBABLE`; el perfil `PROFESIONAL` por sí solo sigue siendo insuficiente. El HTML confirma 23 etiquetas `Agente`.
2. La etiqueta Yapo `Propietario` podía bloquear una identidad directa como `Corredor - Jorge Carreño`. Ahora queda como contradicción visible; el aviso solo pasa a probable si también tiene evidencia directa.
3. Se añadieron firmas de rol vinculadas al publicador, como “corredora de propiedades”, “nuestra corredora” y nombres de contacto que se identifican como corredor.
4. Se reconocen cargos de corretaje como “comisión corredor 2%”, “2% de honorarios de corretaje” y “venta afecta a comisión del 2%”. El cobro solo no clasifica a alguien como corredor; requiere identidad, rol o categoría de anunciante coherente. “Sin comisión” no cuenta como cobro.
5. Se añadió el cierre explícito “atte su dueño” para `DUEÑO_PROBABLE`, si no hay contradicción comercial.
6. La evidencia puede trasladarse solo entre avisos con el mismo `seller_profile_id` exacto. La categoría `Agente` de otro aviso no se propaga, y una etiqueta o declaración de propietario bloquea esa propagación.
7. Se corrigieron falsos desajustes de contacto como “vía WhatsApp” y “al fono”, que no son nombres de personas.

## Señales de propietario y revisión pendiente

La auditoría encontró 6 registros con señales relacionadas con propietario: 3 con declaración directa atribuible, 2 con contacto distinto, 3 con mención de trato directo con dueño(s), y 1 propuesto(s) DUEÑO_PROBABLE. Las categorías se superponen; ver IDs en summary.json.

Los anuncios cuyo único dato es la etiqueta `Propietario`, los títulos genéricos “Vendo casa” y las menciones de comisión sin identidad comercial atribuible permanecen `INCIERTO`. El contacto distinto en una declaración de dueño, las menciones de trato con dueño(s) y los registros sin publicador visible siguen en revisión.

Los 32 `CORREDOR_PROBABLE` requieren confirmación antes de promoverlos a `CORREDOR_SEGURO`; los 59 `INCIERTO` necesitan revisión humana para resolver clasificación. El registro `DUEÑO_PROBABLE` exige verificación de identidad/control antes de considerarlo propietario. El número total de registros sin confirmación final es 92.

## Regresión y catálogo

La suite Yapo contiene regresiones para casos locales con IDs, evidencia positiva/negativa de comisión, perfiles exactos, señales de propietario, contradicciones, `PROFESIONAL` aislado y metadatos `Agente`. No se cambió ni promovió ninguna marca en el catálogo permanente.

Regeneración offline: `python -m scrapers.scraper_yapo.generate_classifier_quality_report`.
