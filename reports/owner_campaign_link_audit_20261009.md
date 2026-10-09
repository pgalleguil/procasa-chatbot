# Auditoría global de enlaces de campaña de ajuste de precio — 09-10-2026

## Resultado de cobertura

- **Enlaces individuales auditados:** 391. Los 391 códigos tienen una propiedad maestra correspondiente, registro de acceso activo, URL única y ruta `/p/{32 caracteres}?source=WHATSAPP`. No hubo claves repetidas ni rutas mal formadas.
- **Estados del ledger:** 393 filas: 355 `SENT`, 3 `DELIVERY_UNKNOWN`, 33 `SKIPPED_STALE_OR_MISMATCH` y 2 `EXCLUDED`. Las 391 primeras filas elegibles cuentan con enlace activo. Se cubrieron las etapas PILOT (10), REMAINDER (270), WAVE2 (111) y EXCLUDED (2).
- **Informes completos en la trazabilidad de correo:** 358. Los artefactos originales se verificaron por tamaño y SHA-256, identidad del código y presencia de acciones de informe, ejecutivo y autorización.
- **Informes incompletos en la trazabilidad de correo:** 33. No existe correo original para estos casos porque el preflight bloqueó su envío. Sus páginas privadas permanecen registradas; no se usaron ni generaron tokens nuevos.
- **Informes con documento primario disponible:** 301 de 391 registros activos. Otros 90 no tienen un PDF primario asociado en el ledger. Esto no equivale a una página vacía: la página puede conservar actividad, exposición, referencias y diagnóstico obtenidos de sus fuentes verificadas.
- **Propiedades/códigos de campaña sin correspondencia en la ficha maestra:** 0.
- **Recomendaciones con aritmética interna inconsistente:** 0 de 391. Los precios propuestos guardados concuerdan con el porcentaje registrado; aun así, eso no resuelve discrepancias entre el precio de campaña y el precio vigente de la ficha.

## Causas encontradas

1. El portal usaba el estado de precio pendiente como bloqueo global del informe. Ese estado ocultaba diagnóstico, comparables, tasación y otras secciones verificables, además de presentar el mensaje ambiguo «Estamos revisando la información comercial de esta propiedad».
2. La autorización consultaba principalmente el estado histórico del envío. No contrastaba de manera uniforme el precio de la campaña con el precio UF vigente de la ficha maestra.
3. Treinta y tres registros quedaron bloqueados durante el preflight. La base conserva el código de error, pero no los valores comparados ni los diagnósticos detallados; por ello no se pueden reconstruir con certeza los valores originales del manifiesto.
4. En dos archivos maestros de Google Sheets se preservan los 391 enlaces y sus filas coinciden. Los ocho archivos individuales de ejecutivos contienen 390 códigos; falta el archivo individual del código 6544, asignado a Pablo Galleguillos. El enlace sí está en ambos maestros.
5. Se revisaron los artefactos de correo disponibles. No se encontró un archivo histórico de despacho de WhatsApp que permita acreditar la entrega individual; sí se comprobaron las rutas WHATSAPP y los códigos de propiedad guardados en las planillas.

## Corrección implementada

- La validación de precio ahora corre por separado del armado del informe. Si falta la ficha, la operación no se puede acreditar, el precio cambió o el cálculo no cuadra, la recomendación queda pendiente y no aparece una acción para autorizar.
- El bloqueo se vuelve a comprobar en el servidor tanto al abrir la confirmación como al enviar una autorización. Un GET o POST no puede registrar una autorización si el precio vigente no coincide.
- La página conserva actividad, publicaciones, documentos y referencias verificadas; si se requiere revisión, muestra las referencias sin posición ni brecha calculada con el precio cuestionado.
- El aviso ahora identifica qué antecedente requiere revisión. Si ya había una autorización, se mantiene registrada y el mensaje lo aclara.
- No se modificaron precios, registros de campaña, planillas, tokens ni envíos. Se conservaron las URL originales.

## Enlaces con recomendación en validación

**Total: 184 códigos.** La unión incluye los 33 bloqueos del preflight y 151 enlaces enviados cuyo precio vigente difiere del precio congelado de campaña. La diferencia por estado es 149 registros `SENT`, 2 `DELIVERY_UNKNOWN` y 6 de los 33 preflight bloqueados; esos seis se cuentan una sola vez.

### Bloqueados por preflight: 33

- `manifest_recommended_price_stale_or_mismatch` (31): 5705, 5708, 5716, 5917, 5918, 5919, 5920, 6007, 6008, 6026, 6030, 6035, 6036, 6264, 6273, 6301, 6420, 6431, 6444, 6445, 6538, 6544, 6545, 6678, 6679, 6691, 6800, 6801, 16544, 16974, 17116
- `manifest_recommendation_stale_or_mismatch` (2): 6792, 6844

Los 33 permanecen en validación hasta recuperar o volver a comprobar sus antecedentes. No se reactivan autorizaciones automáticamente.

### Precio vigente distinto al precio de campaña en enlaces ya enviados: 151

5347, 5612, 5709, 5713, 5763, 5783, 5924, 5931, 5968, 5986, 6005, 6033, 6037, 6042, 6101, 6131, 6170, 6234, 6235, 6255, 6260, 6274, 6318, 6339, 6342, 6366, 6380, 6390, 6412, 6413, 6414, 6418, 6425, 6427, 6447, 6450, 6451, 6455, 6457, 6463, 6472, 6474, 6478, 6481, 6482, 6485, 6488, 6490, 6491, 6493, 6500, 6501, 6502, 6506, 6507, 6537, 6573, 6575, 6577, 6581, 6583, 6590, 6591, 6598, 6601, 6608, 6609, 6622, 6624, 6626, 6628, 6632, 6633, 6634, 6635, 6636, 6639, 6646, 6647, 6648, 6649, 6658, 6660, 6665, 6667, 6668, 6671, 6673, 6677, 6683, 6685, 6690, 6695, 6716, 6717, 6718, 6719, 6720, 6724, 6725, 6726, 6731, 6732, 6753, 6757, 6760, 6761, 6764, 6766, 6769, 6770, 6773, 6774, 6775, 6778, 6780, 6781, 6782, 6787, 6797, 6811, 6829, 6833, 6839, 6843, 6849, 6851, 6852, 6853, 6867, 6871, 6872, 6873, 6875, 6879, 6887, 7390, 16469, 16479, 16486, 16520, 16521, 16523, 16548, 16555, 16588, 16645, 17005, 17048, 17257, 17258

Dos registros de este conjunto, códigos **6673 y 6769**, ya tenían una autorización registrada. La corrección no la revoca ni cambia el precio; el informe indica que la autorización previa permanece registrada y requiere revisión comercial.

## Sin documento PDF primario asociado: 90

5347, 5887, 5968, 6006, 6007, 6008, 6021, 6036, 6042, 6059, 6131, 6132, 6234, 6254, 6311, 6348, 6391, 6444, 6445, 6447, 6450, 6460, 6473, 6477, 6481, 6485, 6488, 6491, 6501, 6502, 6506, 6507, 6512, 6514, 6519, 6536, 6591, 6601, 6632, 6648, 6656, 6670, 6672, 6678, 6679, 6683, 6685, 6718, 6719, 6728, 6729, 6731, 6745, 6748, 6751, 6752, 6760, 6765, 6767, 6770, 6773, 6781, 6787, 6800, 6801, 6810, 6828, 6829, 6833, 6835, 6842, 6853, 6871, 6873, 6888, 16492, 16533, 16544, 16555, 16588, 16671, 16972, 16974, 16982, 16990, 17096, 17116, 17184, 17257, 17258

Se conserva el informe HTML del portal y la información que cada fuente permita verificar; no se presenta un archivo que el ledger no registra.

## Verificación de planillas y preservación

- Los dos maestros contienen 391 filas de datos cada uno; coinciden entre sí, sin códigos duplicados.
- El enlace del código 16544 en la planilla coincide exactamente con el enlace entregado como referencia.
- Los ocho archivos individuales de ejecutivos contienen 390 filas, sin duplicados. El código 6544 está en ambos maestros; no hay archivo individual identificado para Pablo.
- Las siete planillas antiguas de contacto no contienen una columna de enlace y no se consideraron índices de distribución.
- Todas las rutas auditadas mantienen el dominio y token existentes. La corrección no necesita actualizar las planillas.

## Pruebas y límites de auditoría operativa

- Pruebas automatizadas del portal, integridad de precio y autorización: 198 pasaron en la suite enfocada.
- Incluyen 16544, precio vigente cambiado, operación inferida de un bloque de precio, precio UF ausente, inconsistencia aritmética y bloqueo de GET/POST sin registrar una autorización.
- En una ampliación separada de dos suites hubo 65 aprobadas y 7 fallidas en casos del panel QA y una vista de fecha (`tests/test_owner_campaign_admin_panel.py` y `tests/pricing_intelligence/test_owner_portal.py`); esos archivos no forman parte de este cambio. El detalle queda visible en CI, y se informará como limitación de validación.
- Se debe registrar en la entrega final el resultado tras ejecutar la suite final, el commit y la verificación de Render.
- La lectura de las páginas añade un evento de apertura al historial. Para no contaminar las métricas comerciales, no se abrieron masivamente los 391 enlaces; se validaron la relación código-ruta de todos y se hará una apertura de comprobación en producción del enlace original 16544 tras el despliegue.
- No se enviaron correos ni mensajes de WhatsApp, no se ejecutó ninguna acción de autorización y no se cambió ningún precio.

