# Incidente Captaciones Yapo - Junio 2026

## Resumen Ejecutivo

Durante junio de 2026 detectamos que las propiedades captadas desde Yapo.cl no estaban llegando correctamente a los ejecutivos comerciales.

El síntoma inicial fue reportado por Paula Morales, quien tenía configurada la comuna de Talca dentro de sus preferencias, pero no visualizaba propiedades en su bandeja de captación.

Inicialmente se pensó que el problema estaba relacionado con:

* El scraper de Yapo.
* La comuna Talca.
* El estado `gestion.estado = "NUEVO"`.
* La asignación de propietarios directos.

Después de una investigación completa se comprobó que ninguna de esas hipótesis era la causa principal.

---

# Cómo funciona el sistema

## Paso 1 - Scraping

El archivo:

```text
scraping_yapo_proxys.py
```

obtiene propiedades desde Yapo.cl.

Las propiedades se almacenan en:

```text
MongoDB
Colección: yapo_propiedades
```

Cada propiedad contiene información como:

```json
{
  "details": {
    "comuna": "Talca"
  }
}
```

---

## Paso 2 - Distribución

El archivo:

```text
api_captacion.py
```

contiene la función:

```python
distribute_sourced_leads()
```

Esta función busca:

1. Propiedades disponibles.
2. Ejecutivos con comunas configuradas.
3. Coincidencias entre comuna de la propiedad y comuna del ejecutivo.

Si existe coincidencia:

```text
Propiedad Talca
↓
Paula Morales
↓
Asignación
```

---

## Paso 3 - Scheduler

El archivo:

```text
webhook.py
```

ejecuta automáticamente:

```python
captacion_distribution_loop()
```

cada:

```text
3600 segundos
=
1 hora
```

Por lo tanto la distribución NO es instantánea.

Funciona por lotes cada hora.

---

# Problema Detectado

La función:

```python
distribute_sourced_leads()
```

buscaba ejecutivos mediante la consulta:

```python
db["usuarios"].find({
    "comunas_interes": {
        "$exists": True,
        "$not": {"$size": 0}
    }
})
```

Aunque los usuarios sí tenían comunas configuradas, la consulta retornaba:

```text
[]
```

Lista vacía.

Por ejemplo:

```json
{
  "nombre": "Paula Morales",
  "comunas_interes": ["Talca"]
}
```

existía realmente en la base de datos.

Sin embargo la consulta no la encontraba.

---

# Consecuencia

Al no encontrar ejecutivos:

```python
if not ejecutivos_raw:
    return 0
```

la distribución terminaba inmediatamente.

El flujo quedaba así:

```text
Propiedad nueva
↓
Distribuidor busca ejecutivos
↓
Encuentra 0 ejecutivos
↓
return 0
↓
No asigna nada
↓
Los ejecutivos no ven propiedades
```

Esto afectaba a TODOS los ejecutivos.

No solamente a Paula.

---

# Solución Aplicada

Se reemplazó la consulta MongoDB por filtrado directo en Python.

Antes:

```python
ejecutivos_raw = list(
    db["usuarios"].find({
        "comunas_interes": {
            "$exists": True,
            "$not": {"$size": 0}
        }
    })
)
```

Después:

```python
ejecutivos_raw = [
    u for u in db["usuarios"].find()
    if isinstance(u.get("comunas_interes"), list)
    and len(u.get("comunas_interes", [])) > 0
]
```

Con este cambio el sistema volvió a detectar correctamente:

* Paula Morales
* Erika Garrido
* Raquel Cheneaux
* Susana Ensignia
* demás ejecutivos configurados

---

# Resultado de la Corrección

El sistema encontró:

```text
406 propiedades candidatas
```

y asignó:

```text
98 propiedades
```

en la primera ejecución.

Esto confirmó que el problema estaba resuelto.

---

# Segundo Problema Descubierto

Durante la investigación se detectó un problema mucho más grave.

Las propiedades asignadas nunca eran liberadas.

Ejemplo:

```text
Enero
↓
Se asigna a ejecutivo
↓
Nunca registra gestión
↓
Febrero
↓
Marzo
↓
Abril
↓
Sigue asignada
```

No existía ningún mecanismo para recuperar esas oportunidades.

---

# Solución SLA Implementada

Se creó:

```python
release_stale_captaciones()
```

Objetivo:

Liberar propiedades que:

* tengan ejecutivo asignado
* estén en estado NUEVO o GESTION
* no tengan actividad durante 5 días

Cuando ocurre:

```text
5 días sin gestión
↓
Se libera
↓
Vuelve al pool
↓
Puede asignarse a otro ejecutivo
```

---

# Resultado SLA

Se liberaron:

```text
386 captaciones
```

que llevaban meses sin movimiento.

Posteriormente esas captaciones vuelven a ser redistribuidas automáticamente.

---

# Funcionamiento Actual

## Propiedad nueva

```text
Scraper
↓
MongoDB
↓
Distribución cada 1 hora
↓
Ejecutivo asignado
↓
Visible en captación
```

---

## Propiedad abandonada

```text
Ejecutivo asignado
↓
5 días sin gestión
↓
Liberación automática
↓
Redistribución
↓
Nuevo ejecutivo
```

---

# Archivos Modificados

## api_captacion.py

Cambios:

* Corrección búsqueda de ejecutivos.
* Nueva función release_stale_captaciones().
* Logs adicionales.

## webhook.py

Cambios:

* Integración de release_stale_captaciones().
* Ejecución automática antes de cada distribución.

---

# Estado Final

Estado del incidente:

```text
RESUELTO
```

Verificaciones realizadas:

✅ Ejecutivos detectados correctamente.

✅ Paula Morales detectada correctamente.

✅ Distribución funcionando.

✅ Scheduler funcionando.

✅ Captaciones nuevas se asignan.

✅ Captaciones antiguas se liberan.

✅ Redistribución automática operativa.

---

# Qué revisar en el futuro

Si algún ejecutivo vuelve a reportar que no recibe captaciones:

1. Revisar logs de distribute_sourced_leads().
2. Verificar cantidad de ejecutivos detectados.
3. Verificar cantidad de propiedades candidatas.
4. Revisar captacion_distribution_loop().
5. Verificar que existan comunas_interes configuradas.
6. Verificar que la propiedad tenga comuna válida.

Mientras los logs indiquen:

```text
[DISTRIBUCION] Ejecutivos detectados
[DISTRIBUCION] Propiedades candidatas
[DISTRIBUCION] Asignadas
```

el sistema se considera operativo.
