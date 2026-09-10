# Preparación futura de ML del Portal del Propietario

Esta fase no entrena modelos, no calcula pronósticos y no persiste eventos de simulación.
`DemandForecastViewV1` permanece como contrato futuro no instanciado y oculto.

## Estado de suficiencia

- SUCRE: 459 propiedades vigentes.
- Leads exactos vinculados: aproximadamente 826 o más, según la auditoría disponible.
- Bajas significativas históricas identificadas: 11 propiedades y 19 eventos.
- El histórico prospectivo está recién iniciado; todavía no existe un panel diario estable.
- Estado: `FORECAST_EXPLORATORY_NOT_READY_FOR_PRODUCTION`.
- Estado del simulador causal: `PRICE_CAUSAL_SIMULATOR_NOT_READY`.

## Contrato futuro

`PriceResponseSimulationV1` queda definido con:

`scenario_price`, `expected_inquiries_30d`, `baseline_expected_inquiries_30d`,
`delta_expected`, `lower_bound`, `upper_bound`, `model_version`, `training_cutoff` y
`confidence_status`.

El contrato no ejecuta cálculo ni aparece en la vista actual. La V1 solo compara posición
descriptiva contra publicaciones similares y no interpreta demanda.

## Variables a capturar diariamente

La unidad futura será `property_code × week`, alimentada por snapshots diarios de:

`property_code`, `price`, `comparable_percentile`, `uf_m2`, `market_median`, `leads`,
`portals`, `market_supply`, `mortgage_indicators` y `price_changes`.

Cada captura deberá conservar fecha, fuente, disponibilidad, exposición y reglas de
vinculación para evitar mezclar períodos o atribuir causalidad sin diseño experimental.

## Eventos futuros de producto

Se reservan `price_scenario_changed` y `price_scenario_preset_selected` para una fase
posterior. El slider no persiste cada movimiento en esta fase. Cuando se habilite la
medición, el movimiento continuo deberá agruparse con debounce/throttle y solo registrar
interacciones significativas, junto con aperturas, retornos, solicitudes de contacto y
aceptación de ajustes.
