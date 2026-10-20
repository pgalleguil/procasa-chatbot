# Motor de recomendaciones comerciales: piloto RAG híbrido

## Objetivo

Si un cliente preguntó por la propiedad A y no avanzó comercialmente, producir
alternativas B/C/D de la cartera actual. No es un motor de tasación ni utiliza
K-Means. Opera sobre fichas de universo_cartera_prop360 y preferencias que el
chatbot ya haya guardado de forma estructurada en los leads.

## Diseño y límites reales del primer corte

- Mismo modelo de embeddings precomputados (vector_descripcion, 384 dimensiones)
  del chatbot. Compara vector A con vectores de otras fichas por coseno. No
  genera vectores nuevos ni carga FastEmbed en el servidor.
- Recuperación por reglas: disponible_prop360, oficina, operación, tipo, comuna,
  precio y dormitorios/baños mínimos SOLO si fueron confirmados por el lead.
- Ampliación a comunas vecinas opcional (no por defecto). La red de oficinas
  también requiere un parámetro explícito.
- Ranking: coseno semántico 40%; similitud léxica 10%; cercanía precio 20%;
  superficie 12%; dormitorios 8%; misma comuna 10%.
- Los valores desconocidos no se dan por cumplidos para filtros duros.
- No convertir las características de A en preferencias obligatorias sin
  confirmación del comprador.
- Las fichas sin un código verificado no se usan como origen. En fichas con
  operación de venta y arriendo simultáneamente, el sistema se abstiene
  hasta determinar la operación real.
- URL devuelta solo cuando ya existe en la ficha con código y dominio válidos.
- Resultado incluye motivos, puntaje y bandera requires_manual_review.
  El puntaje NO es probabilidad de conversión.

**No está implementado aún:** extracción de preferencias de conversaciones
completas, validación de consentimiento final, integración visual CRM,
notificaciones, envío de WhatsApp, medición de conversión o un índice Atlas
Vector Search. Esta versión es un piloto de cálculo batch en memoria, apropiado
para una oficina de cientos de propiedades; se debe medir coste y latencia al
escalar la red.

## CLI sin escrituras

Desde la raíz del repo, con MONGO_URI configurado:

    python -m scripts.commercial_recommendations_dry_run --limit-leads 100
    python -m scripts.commercial_recommendations_dry_run --limit-leads 100 --neighbors --output /tmp/recomendaciones.json

--network amplía expresamente a otras oficinas. --neighbors permite alternativas
en comunas vecinas cuando la comuna original no completa el cupo.
Límite permitido: 1 a 1000 leads. CLI solo usa find(), nunca update(), insert()
o clientes de WhatsApp. La salida omite nombres, teléfonos y mensajes.

## Validaciones necesarias antes de una campaña

1. Validar matches y vínculos de property_code contra alias e históricos;
   detectar códigos ambiguos y publicaciones retiradas.
2. Verificar los precios por operación y disponibilidad al momento de enviar.
3. Extraer preferencias del historial conversacional con contexto y confirmación
   del usuario: presupuestos, zonas aceptadas, rechazos, interés en visitas.
4. Auditar opt-out y consentimiento en todas las fuentes de mensajes.
5. Evaluar Precision@3, proporción con 1/3/5 alternativas, casos falsos,
   compatibilidad de precio, visitas, ofertas y ventas atribuibles.
6. Vincular responsable actual del lead sin reasignaciones automáticas.
7. Activar envío solo tras validación de contenido y controles antiduplciado.

No hay despliegue, escrituras de Mongo ni envíos en este piloto.
