# Auditorías y Saneamientos — Clasificación Dueño vs Corredor

Scripts para auditar, diagnosticar y corregir la clasificación de propietarios y corredores en `URLS.yapo_propiedades`. No hay que rehacer el trabajo desde cero ante futuros problemas.

---

## Scripts de Diagnóstico (Solo Lectura)

| Script | Para qué sirve |
|---|---|
| `audit_today_profile_id.py` | Auditoría general del día: totales, distribución dueños/corredores y señales activas |
| `audit_classification_reasons.py` | Detalla por qué cada corredor fue clasificado así (señales booleanas) |
| `audit_company_contamination.py` | Detecta el bug de contaminación: `company_name == publicador` sin palabras empresariales |
| `audit_profile_signal_validity.py` | Cruza MongoDB con HTML crudos para validar si `seller_profile_id` tiene badge real "Profesional" |
| `audit_fake_owners.py` | Detecta corredores camuflados como dueños: multi-publicadores, nombres genéricos y lenguaje sospechoso en descripción |
| `verify_post_fix_status.py` | Validación post-corrección: confirma que los fixes aplicaron bien |

---

## Scripts de Corrección (Modifican MongoDB)

| Script | Para qué sirve |
|---|---|
| `fix_historical_all.py` | **El principal.** Recorre toda la historia y corrige falsos positivos de dueños. Cubre ambos bugs: `company_name` y `profile_id`. Usar cuando haya una corrección masiva. |

---

## Lógica de Correcciones en el Scraper

Todas las siguientes correcciones ya están implementadas directamente en `scraping_yapo_proxys.py`:

- **`seller_profile_id` invalidado:** Ya no suma puntos en `is_likely_broker()`. Yapo lo asigna a todos los usuarios.
- **`seller_is_pro` limpio:** Solo es `True` con badge visual "Profesional" real en el HTML.
- **`company_name` seguro:** Solo se copia desde `publicador` si el nombre contiene palabras de `_BROKER_KEYWORDS`.
- **"Agente" / "Vendedor":** Nombre default de Yapo → siempre clasificado como corredor.
- **"sin comisión":** No se confunde con avisos de corredores que cobran comisión.
- **Post-proceso multi-publicador:** Al terminar cada sesión de scraping, se reclasifican automáticamente los "dueños" con 5+ propiedades activas.

---

*Ante cualquier anomalía futura: primero ejecutar `audit_today_profile_id.py` y `verify_post_fix_status.py`. Solo si hay un problema confirmado, usar `fix_historical_all.py`.*
