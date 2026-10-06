# Portal brand assets

These files are local copies of portal brand marks used by the owner portal.
The portal template references only `/static/...` assets at runtime.

| Channel | Local asset | Source |
| --- | --- | --- |
| PROCASA | `../favicon_procasa_mark.png` | Existing repository asset |
| Portal Inmobiliario | `portalinmobiliario.svg` | `https://http2.mlstatic.com/frontend-assets/pi-web-navigation/ui-navigation/6.9.2/portalinmobiliario/favicon.svg` |
| Mercado Libre | `mercado-libre.svg` | `https://http2.mlstatic.com/frontend-assets/ml-web-navigation/ui-navigation/5.21.22/mercadolibre/favicon.svg` |
| TocToc | `toctoc.svg` | `https://d2jd36q67phkec.cloudfront.net/toctoc/img/logos/brand/logo-tt-h.svg` |
| Yapo | `yapo.svg` (embedded local copy, icon view) | `https://getonbrd-prod.s3.amazonaws.com/uploads/users/logo/253/logo_yapo_2022.png` |
| Proppit | `proppit.png` | `https://www.proppit.com/proppit-favicons/mstile-150x150.png` |
| ChilePropiedades | `chilepropiedades.svg` | `https://chilepropiedades.cl/assets/images/favicon.svg` |
| Enlace Inmobiliario | `enlace-inmobiliario.png` | Local crop of the Enlace Inmobiliario mark from the official Enlace BCI page asset: `https://www.enlaceinmobiliarios.cl/bci/bancarios/img/logos_portales/banco_87_color.png` |

Names and marks remain the property of their respective owners. Yapo's 200×200
transparent company mark is embedded in the local `yapo.svg`, which shows only
its high-resolution cube to match the existing icon-only row. Image elements use
`object-fit: contain` so the source aspect ratios are preserved.
