# Runbook anterior: reemplazado

Este runbook describía el circuito V1 `crm_price_drop_campaigns_v1`, con otra colección y otro ejecutor. Ese flujo ya no es un punto operativo para campañas de clientes. La operación actual de piloto y cartera está documentada en [price_drop_campaign_operations.md](price_drop_campaign_operations.md) y utiliza exclusivamente `scripts/enviar_piloto_rebajas.py`, con registros bajo `leads.price_drop_v2.campaigns`.

Los scripts antiguos conservados como stubs responden `BLOCKED` y no abren MongoDB ni llaman al proveedor. No se deben reactivar mediante variables de arranque.
