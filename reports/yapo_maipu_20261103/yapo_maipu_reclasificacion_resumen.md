# Reclasificación offline Yapo Maipú

**Modo:** simulación local. Sin scraping, sin lectura/escritura de MongoDB y sin llamadas ni tokens de DeepSeek.

## Resultado

- Avisos auditados: 482; candidatos del Excel corregido: 134.
- Catálogo conocido: 1276 nombres, 1276 normalizados únicos.
- Original → nuevo: {'CORREDOR_SEGURO': 313, 'INCIERTO': 148, 'CORREDOR_PROBABLE': 18, 'DUEÑO_PROBABLE': 3} → {'CORREDOR_PROBABLE': 350, 'CORREDOR_SEGURO': 2, 'INCIERTO': 125, 'DUEÑO_PROBABLE': 5}.
- Corredores resueltos sin DeepSeek: 352; dueños probables: 5; casos ambiguos candidatos a IA: 130 (simulación, cero llamadas).
- De los 148 originales INCIERTO, 23 se resolvieron automáticamente con reglas determinísticas.
- Enrutamiento simulado: 352 determinísticos y 130 casos ambiguos; ahorro estimado de llamadas: 73.03%.
- Evidencia ausente en 97 candidatos: 97 tienen título y descripción extraídos, pero sin señales concluyentes; se conserva INCIERTO y queda explicado en el CSV. Descripciones faltantes: 0; publicador faltante: 2.
- Diagnóstico de las 129 filas del CSV anterior: evidencia vacía 86; títulos faltantes 0, descripciones faltantes 0, publicadores faltantes 2.
- Precios CLP del CSV base de 129 candidatos: 60 validados con UF $ 40,844.79 al 2026-11-03; dentro del rango: 60, fuera: 0. Fuente: https://www.sii.cl/valores_y_fechas/uf/uf2026.htm.
- Identidades fuera del catálogo registradas: 24 candidatas (8 con señales de corredor más fuertes); ninguna se promovió automáticamente.
- Excel corregido: 2 corredores ya separados; 14 exclusiones nuevas; 120 candidatos quedan en el CSV priorizado.
- Reclasificaciones: 336; ejemplos de falsos negativos previos corregidos: 32200413 (VISAN) y 27979919 (Consultora Inmobiliaria).
- Falsos positivos confirmados: 0 detectados en esta inspección. No hay etiquetas humanas completas, por lo que esto no equivale a una tasa de error medida.

## Tokens y costo hipotético

- Tokens de entrada aproximados: 344,277 si se enviaran los 482; 76,070 si se enviaran solo los ambiguos; ahorro aproximado: 268,207.
- Costo pico hipotético: US$0.392483 para los 482 frente a US$0.100821 para los 130 ambiguos; US$0.291662 ahorrados. Estimación por 4 caracteres/token y máximo de 500 tokens de salida por llamada; precio sujeto a cambios.
- Tarifas usadas: [documentación oficial de DeepSeek](https://api-docs.deepseek.com/quick_start/pricing/) (Flash, hora pico, entrada sin caché US$0.30/M y salida US$1.20/M).

## Archivos

- Auditoría completa con cambios por ID y evidencia: `C:\Users\pgall\Documents\PROCASA\YapoMaipu\yapo_maipu_reclasificacion_offline_auditoria.json`
- CSV priorizado con descripciones completas: `C:\Users\pgall\Documents\PROCASA\YapoMaipu\yapo_maipu_candidatos_priorizados.csv`
- Registro de identidades candidatas pendientes de verificación: `C:\Users\pgall\Documents\PROCASA\YapoMaipu\unknown_broker_candidates.json`
