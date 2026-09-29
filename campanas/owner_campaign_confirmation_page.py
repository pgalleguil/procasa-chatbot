from __future__ import annotations

from html import escape


def _document(title: str, subtitle: str, content: str, logo_url: str = "") -> str:
    brand = (
        f'<img class="brand-logo" src="{escape(logo_url, quote=True)}" alt="PROCASA">'
        if logo_url else '<div class="brand">PROCASA</div>'
    )
    return (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>PROCASA | {escape(title)}</title><style>'
        '*{box-sizing:border-box}body{margin:0;background:#f5f6fb;color:#171b4b;'
        'font-family:Arial,Helvetica,sans-serif}.shell{max-width:920px;margin:0 auto;padding:48px 24px 56px}'
        '.brand{font-size:19px;font-weight:800;letter-spacing:.08em;color:#211b65}.brand-logo{display:block;max-width:156px;max-height:38px;width:auto;height:auto;object-fit:contain;object-position:left center}'
        'h1{font-size:32px;line-height:1.2;margin:22px 0 8px;letter-spacing:-.025em}'
        '.subtitle{font-size:16px;line-height:1.55;color:#68708f;margin:0 0 30px}'
        '.options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;align-items:stretch}'
        '.card{background:#fff;border:1px solid #e1e4f1;border-radius:18px;padding:25px;box-shadow:0 8px 24px rgba(24,29,82,.055)}'
        '.card.primary{border:2px solid #6253d7;box-shadow:0 12px 30px rgba(73,61,171,.12)}'
        '.eyebrow{font-size:11px;font-weight:800;letter-spacing:.12em;color:#6556d9}'
        '.card h2{font-size:21px;margin:13px 0 18px;color:#171b4b}'
        '.percent{font-size:48px;font-weight:800;letter-spacing:-.04em;line-height:1;color:#211b65;margin:0 0 23px}'
        '.value-label{display:block;font-size:10px;font-weight:800;letter-spacing:.11em;color:#79809b;margin-bottom:6px}'
        '.price{font-size:22px;font-weight:800;color:#211b65;line-height:1.35}'
        '.copy{font-size:14px;line-height:1.6;color:#626b88;margin:16px 0 21px}'
        'form{margin:0}.button{display:flex;align-items:center;justify-content:center;min-height:48px;width:100%;padding:12px 16px;border:0;border-radius:10px;background:#332a91;color:#fff;text-decoration:none;text-align:center;font-size:12px;font-weight:800;letter-spacing:.035em;cursor:pointer}'
        '.secondary .button{background:#fff;color:#342d85;border:1px solid #c8c5e8}'
        '.contact{margin-top:20px;padding:22px 24px;background:#fff;border:1px solid #e1e4f1;border-radius:16px}'
        '.contact h2{font-size:18px;margin:0 0 8px}.contact p{color:#68708f;font-size:14px;line-height:1.55;margin:0 0 16px}'
        '.link{display:inline-block;color:#4134b2;text-decoration:none;font-weight:800;font-size:12px;letter-spacing:.035em}'
        '.steps{margin-top:26px;padding:22px 24px;background:#f0f2fa;border-radius:16px}.steps h2{font-size:16px;margin:0 0 14px}'
        '.steps ol{margin:0;padding-left:21px;color:#59617d}.steps li{padding:5px 0 5px 4px;font-size:13px;line-height:1.5}'
        '.result{max-width:660px;margin:26px auto 0;padding:30px;background:#fff;border:1px solid #e1e4f1;border-radius:18px;box-shadow:0 8px 24px rgba(24,29,82,.055)}'
        '.result h2{font-size:11px;letter-spacing:.12em;color:#6556d9;margin:0 0 7px}.result .price{margin:0 0 24px}'
        '.result .split{display:grid;grid-template-columns:1fr 1fr;gap:15px;border-top:1px solid #e8e9f2;padding-top:20px;margin-top:20px}'
        '.already{margin-top:18px;padding:15px;background:#f4f3fc;border-radius:10px;color:#4e5070;font-size:14px;line-height:1.6}'
        '@media(max-width:600px){.shell{padding:28px 16px 36px}h1{font-size:27px;margin-top:18px}.subtitle{font-size:15px;margin-bottom:22px}.options{grid-template-columns:1fr;gap:14px}.card{padding:21px;border-radius:15px}.percent{font-size:43px}.contact{padding:20px}.steps{padding:20px}.result{padding:23px 20px}.result .split{grid-template-columns:1fr}}'
        f'</style></head><body><main class="shell"><header>{brand}'
        f'<h1>{escape(title)}</h1><p class="subtitle">{escape(subtitle)}</p></header>{content}'
        '</main></body></html>'
    )


def render_decision_page(
    *, recommended_pct: int, recommended_price: str, current_price: str,
    gradual_pct: int | None, gradual_price: str, recommended_url: str,
    gradual_url: str, advisor_url: str, logo_url: str = "",
) -> str:
    gradual_available = gradual_pct is not None and int(gradual_pct) != int(recommended_pct)
    recommendation = (
        '<section class="card primary"><div class="eyebrow">RECOMENDACIÓN PROCASA</div>'
        '<h2>Ajuste recomendado</h2>'
        '<span class="value-label">PRECIO ACTUAL</span>'
        f'<div class="price" style="font-size:17px;margin:0 0 20px">{current_price}</div>'
        f'<p class="percent">{int(recommended_pct)}%</p>'
        '<span class="value-label">NUEVO VALOR SUGERIDO</span>'
        f'<div class="price">{recommended_price}</div>'
        '<p class="copy">Esta recomendación considera principalmente la respuesta comercial de tu '
        'propiedad durante los últimos 90 días, complementada por las referencias de mercado y '
        'antecedentes disponibles.</p>'
        f'<form method="post" action="{escape(recommended_url, quote=True)}"><button class="button" type="submit">'
        'AUTORIZAR AJUSTE RECOMENDADO</button></form></section>'
    )
    if gradual_available:
        option = (
            '<section class="card secondary"><div class="eyebrow">OPCIÓN GRADUAL</div>'
            '<h2>Ajuste gradual</h2>'
            f'<p class="percent">{int(gradual_pct)}%</p>'
            '<span class="value-label">NUEVO VALOR SUGERIDO</span>'
            f'<div class="price">{gradual_price}</div>'
            '<p class="copy">Si prefieres avanzar con un ajuste más moderado, esta alternativa permite '
            'mejorar el posicionamiento de forma gradual, manteniendo abierta la posibilidad de revisar '
            'nuevamente su desempeño.</p>'
            f'<form method="post" action="{escape(gradual_url, quote=True)}"><button class="button" type="submit">'
            'AUTORIZAR AJUSTE GRADUAL</button></form></section>'
        )
    else:
        option = (
            '<section class="card secondary"><div class="eyebrow">OPCIÓN PERSONALIZADA</div>'
            '<h2>¿Tienes otra propuesta?</h2><p class="copy">Si deseas proponer un ajuste distinto, '
            'puedes conversarlo directamente con tu ejecutivo.</p>'
            f'<a class="button" href="{escape(advisor_url, quote=True)}">QUIERO PROPONER OTRO AJUSTE</a></section>'
        )
    content = (
        f'<div class="options">{recommendation}{option}</div>'
        '<section class="contact"><h2>¿Prefieres conversarlo antes de decidir?</h2>'
        '<p>Si deseas revisar la propuesta antes de autorizar un cambio, puedes solicitar que tu ejecutivo te contacte.</p>'
        f'<a class="link" href="{escape(advisor_url, quote=True)}">SOLICITAR CONTACTO DE MI EJECUTIVO →</a></section>'
        '<section class="steps"><h2>¿Qué ocurrirá después?</h2><ol>'
        '<li>Tu decisión quedará registrada de forma segura.</li>'
        '<li>Tu ejecutivo será informado automáticamente.</li>'
        '<li>El precio solo será actualizado después de validar internamente tu autorización.</li>'
        '</ol></section>'
    )
    return _document("Confirma tu ajuste de precio", "Selecciona la opción que mejor se ajuste a tu decisión.", content, logo_url)


def render_success_page(
    *, selected_type: str, selected_pct: int | None, selected_price: str,
    recommended_pct: int | None = None, recommended_price: str = "",
    advisor_url: str = "", already_registered: bool = False, logo_url: str = "",
) -> str:
    selected_type = str(selected_type or "").upper()
    if already_registered:
        title = "Tu autorización ya fue registrada"
        subtitle = "Tu decisión quedó guardada de forma segura."
        body = (
            '<div class="already">Tu autorización ya fue registrada. Si deseas modificarla, '
            f'<a class="link" href="{escape(advisor_url, quote=True)}">solicita contacto con tu ejecutivo</a>.</div>'
        )
    elif selected_type == "ADVISOR_REVIEW":
        title = "Solicitud enviada"
        subtitle = "Hemos registrado tu solicitud. Tu ejecutivo será informado para que pueda contactarte y revisar la propuesta contigo."
        body = '<section class="result"><h2>SOLICITUD REGISTRADA</h2><p class="copy">Tu ejecutivo se pondrá en contacto contigo para revisar la propuesta.</p></section>'
    elif selected_type == "GRADUAL":
        title = "Ajuste gradual autorizado"
        subtitle = "Hemos registrado el ajuste gradual que seleccionaste."
        body = (
            '<section class="result"><div class="split"><div><h2>RECOMENDACIÓN PROCASA</h2>'
            f'<div class="price">{int(recommended_pct or 0)}% → {recommended_price}</div></div>'
            '<div><h2>TU AJUSTE AUTORIZADO</h2>'
            f'<div class="price">{int(selected_pct or 0)}% → {selected_price}</div></div></div>'
            '<p class="copy">Tu ejecutivo será informado y revisará la actualización antes de que el cambio se vea reflejado en la publicación.</p></section>'
        )
    else:
        title = "Ajuste autorizado correctamente"
        subtitle = "Hemos registrado tu autorización para actualizar el precio según la recomendación de PROCASA."
        body = (
            '<section class="result"><h2>AJUSTE AUTORIZADO</h2>'
            f'<p class="percent">{int(selected_pct or 0)}%</p><h2>NUEVO VALOR AUTORIZADO</h2>'
            f'<div class="price">{selected_price}</div>'
            '<p class="copy">Tu ejecutivo será informado y revisará la actualización antes de que el cambio se vea reflejado en la publicación.</p></section>'
        )
    return _document(title, subtitle, body, logo_url)
