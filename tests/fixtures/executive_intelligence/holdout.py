"""Frozen wording challenge, authored after implementation; do not tune on it.

Synthetic, 70 independent utterances, no population accuracy claim.
"""
from catalog import turn

TEXTS = {
 'ASK_INFO': ['¿A cuánto está el depa?', '¿Cuánto se paga de contribuciones?', 'Mándame la ficha porfa', 'Necesito saber si aceptan mascotas', '¿Tiene ascensor ese edificio?'],
 'INTERESTED': ['Está buena la casa, me tinca', 'Me gustó bastante ese departamento', 'Sí, me interesa avanzar', 'La encuentro ideal para nosotros', 'Estoy interesada en verla con calma'],
 'ASK_VISIT': ['¿La podemos ir a ver el viernes?', 'Me gustaría conocer la propiedad', '¿Hay disponibilidad para verla mañana?', 'Quisiera agendar una visita', 'Podría ir a ver el departamento el lunes'],
 'ASK_CONTACT': ['¿Podríamos hablar por teléfono?', 'Prefiero que me llames', 'Que me contacte alguien de ventas por favor', 'Necesito conversar con un asesor', '¿Puedes llamarme a las cinco?'],
 'GIVE_OFFER': ['Puedo ofrecer 2800 UF por la casa', 'Les hago una oferta por 90 millones', 'Presento una oferta de 3000 UF', 'Ofrezco 3.200 UF, con crédito aprobado', 'Quiero ofertar 2500 UF al contado'],
 'ASK_ALTERNATIVES': ['¿Tienes algo más barato?', 'Esa no, mejor muéstrame otra', 'Mándame opciones parecidas por Maipú', '¿Qué otras casas tienen por el sector?', 'Busco otra propiedad con patio'],
 'PROPERTY_DECLINED': ['No me gustó esa casa', 'Ese departamento no me sirve', 'Descarto esa propiedad por el ruido', 'La casa es muy chica para nosotros', 'No me interesa ese departamento'],
 'SEARCH_COMPLETED': ['Ya arrendamos una casa con otra corredora', 'Encontré lo que buscaba, gracias', 'La búsqueda ya terminó', 'Ya resolví mi compra', 'Ya no busco departamento'],
 'OPT_OUT': ['Bórrenme de su lista de contactos', 'No me vuelvan a escribir', 'Dejen de mandarme publicidad', 'No deseo más comunicaciones de PROCASA', 'No me escribas más'],
 'SEARCH_UPDATED': ['Ahora prefiero arrendar en vez de comprar', 'Subí el presupuesto a 4500 UF', 'Necesito al menos tres dormitorios', 'Busco un departamento en Ñuñoa', 'Busco casa con patio, ahora en Maipú'],
 'OWNER_SELL': ['Soy dueño y quiero vender', 'Tengo una casa que quiero poner en venta', 'Busco una corredora para vender mi departamento', 'Me ayudan a vender una propiedad heredada', 'Necesito vender mi casa de Maipú'],
 'OWNER_RENT': ['Soy propietario y quiero poner mi departamento en arriendo', 'Quiero que arrienden mi casa', 'Tengo una oficina para arrendar a terceros', 'Necesito ofrecer mi casa en arriendo', 'Quiero alquilar mi departamento'],
 'MIXED_NEEDS': ['Primero vendo mi casa y luego compro algo más chico', 'Tengo que vender para financiar la compra de un departamento', 'Quiero vender mi casa para comprar otra', 'Busco comprar pero antes debo vender mi terreno', 'Me interesa vender y comprar al mismo tiempo'],
 'NO_SIGNAL': ['Si quisiera visitar, ¿con quién tendría que hablar?', 'Mi oferta sería 3000 UF, era un ejemplo', 'No me contacten más por esa casa, sí por otras', 'No dije que quiero ofertar', 'Claro, como si ya hubiera comprado una casa'],
}


def cases():
 return [{'id': f'holdout-{label}-{n}', 'family': f'holdout-{label}-{n}',
          'intent': label, 'variant': 0, 'messages': [turn('customer', text, 1)]}
         for label, texts in TEXTS.items() for n, text in enumerate(texts)]
