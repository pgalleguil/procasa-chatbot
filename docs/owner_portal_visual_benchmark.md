# Benchmark visual del Portal del Propietario

Benchmark breve para orientar tres prototipos independientes. No se copian assets,
layout ni código; se extraen principios de interacción, jerarquía y comunicación.

Fuentes revisadas: [Homebot Home Digest](https://help.homebotapp.com/en/articles/3975961-understanding-the-digest),
[PriceHubble Property Advisor](https://www.pricehubble.com/products/property-advisor),
[Street.co.uk Vendor Experience](https://api.street.co.uk/solutions/single-branch),
[Zillow Listing Performance](https://grow.zillow.com/hubfs/Playbook/Listing%20Performance.pdf),
[Linear UI refresh](https://linear.app/now/behind-the-latest-design-refresh),
[Linear Method](https://linear.app/method/introduction),
[Wealthfront Investing](https://www.wealthfront.com/investing),
[Stripe Dashboard](https://docs.stripe.com/dashboard/basics),
[Stripe Apps design](https://docs.stripe.com/stripe-apps/design),
[Vercel Geist](https://vercel.com/geist/introduction),
[Apple Design Resources](https://developer.apple.com/design/resources/).

## Principios detectados

1. **Primero una respuesta, después el detalle.** Homebot parte con un digest personalizado y deja que el propietario vuelva periódicamente a revisar cambios.
2. **La propiedad es el ancla emocional.** PriceHubble combina información del inmueble, comparables, barrio y contexto para convertir datos en una conversación comprensible.
3. **La gestión debe ser visible.** Street.co.uk convierte servicio y seguimiento en una experiencia que el cliente puede usar, no solo en una pantalla interna del agente.
4. **Los gráficos deben explicar una decisión.** Zillow usa actividad, comparación y cambios relativos con etiquetas concretas; no depende de ornamentación.
5. **La jerarquía se gana.** Linear reserva el mayor peso visual para la tarea principal y relega navegación, iconos y metadatos.
6. **La estructura se siente, no se acumula.** Linear recomienda separadores suaves y menos tratamientos para evitar que cada dato parezca una caja independiente.
7. **El detalle aparece bajo demanda.** Digital reports y experiencias de advisory pueden empezar con una lectura simple y abrir profundidad contextual cuando el usuario la necesita; Wealthfront refuerza esta progresión con una lectura de patrimonio que se puede explorar sin convertirla en un formulario.
8. **La confianza necesita trazabilidad.** Fechas, fuentes y definiciones deben acompañar cada lectura de mercado sin invadir el primer plano.
9. **Mobile no es una versión reducida.** La lectura inicial debe funcionar con un pulgar, una cifra principal y una progresión corta; los gráficos deben conservar su significado a 390px. Vercel aporta la disciplina de un sistema tipográfico y de contraste consistente, y Apple la referencia de storytelling visual con producto como protagonista.
10. **Volver debe tener un motivo.** Un componente “desde tu última actualización” solo aparece cuando existe un estado anterior real y convierte el portal en seguimiento, no en un informe estático.

## Traducción a los prototipos

- **A · Cinematic Property Story:** emoción, fotografía, relato vertical y cifras sobrepuestas.
- **B · Owner Command Center:** control, estado actual, actividad verificable y módulos de seguimiento.
- **C · Market Intelligence Experience:** evidencia, distribución de precios, rango local y posición de la propiedad.

Los tres reutilizan el mismo DTO real. El simulador, los pronósticos, las recomendaciones y
las acciones de autorización quedan fuera de los tres conceptos.
