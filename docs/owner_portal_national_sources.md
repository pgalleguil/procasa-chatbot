# Fuentes nacionales evaluadas para Owner Portal

Fecha de revisión: 11/09/2026  
Alcance: evaluación de fuentes públicas; sin escrituras MongoDB y sin consultas desde el pageview.

| Indicador | Fuente | Serie / endpoint | Automatizable | Estado |
|---|---|---|---|---|
| Tasa promedio de créditos de vivienda en UF, más de 3 años | Banco Central de Chile, BDE | `F022.VIV.TIP.MA03.UF.Z.M` vía `https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx` | Sí, con token BDE | Preparado en `market_intelligence.sources`; no configurado |
| Índice de precios de vivienda (IPV) | Banco Central de Chile, BDE | `F034.IPV.FLU.BCCH.2008.0.T` vía el mismo endpoint | Sí, con token BDE | Preparado; no configurado |
| IPC mensual | Instituto Nacional de Estadísticas, serie publicada en BDE | `F074.IPC.VAR.Z.2023.C.M` vía el mismo endpoint | Sí, con token BDE | Preparado; no configurado |

La BDE documenta la tasa hipotecaria como una serie mensual de operaciones efectivas
pactadas en UF y a más de tres años. El IPV se difunde trimestralmente y se construye
con registros administrativos innominados del SII; por eso se considera contexto nacional,
no una medida de la propiedad individual.

## Credencial requerida

El endpoint BDE requiere un token de acceso para `GetSeries`. La ingesta espera la variable
de entorno `BCCH_BDE_API_TOKEN`; no se inventan credenciales ni se guardan tokens en código.
Sin esa variable, el dry-run devuelve `blocked_missing_api_token`, con cero escrituras y
cero llamadas desde el portal.

## Arquitectura aprobada

`fuente oficial → ingesta periódica/dry-run → snapshot interno → portal`

El portal solo lee `market_intelligence_snapshots_v1` y `uf_cache`. El bloque nacional se
renderiza únicamente cuando existen al menos dos indicadores nacionales válidos y fechados.
La UF aislada no se presenta como análisis nacional completo.
