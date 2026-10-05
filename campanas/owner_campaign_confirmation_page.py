from __future__ import annotations

from html import escape
import os


def gradual_option_enabled(campaign_id: str | None = None) -> bool:
    """Gradual pricing remains available for future campaigns, disabled by default."""
    if str(campaign_id or "").startswith("owner_price_sucre_wave1"):
        return False
    return str(os.getenv("OWNER_CAMPAIGN_GRADUAL_OPTION_ENABLED", "false")).strip().casefold() in {"1", "true", "yes", "on"}


def _document(title: str, subtitle: str, content: str, logo_url: str = "") -> str:
    brand = (
        f'<img class="brand-logo" src="{escape(logo_url, quote=True)}" alt="PROCASA">'
        if logo_url else '<div class="brand">PROCASA</div>'
    )
    return (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="icon" type="image/png" href="/static/favicon_procasa_mark.png?v=1.0.13">'
        '<link rel="apple-touch-icon" href="/static/favicon_procasa_mark.png?v=1.0.13">'
        f'<title>PROCASA | {escape(title)}</title><style>'
        '*{box-sizing:border-box}body{margin:0;background:#f5f6fb;color:#171b4b;'
        'font-family:Arial,Helvetica,sans-serif}.shell{max-width:920px;margin:0 auto;padding:24px 24px 28px}'
        'header{text-align:center}.brand{display:block;margin:0 auto 22px;text-align:center;font-size:22px;font-weight:800;letter-spacing:.08em;color:#211b65}.brand-logo{display:block;width:auto;max-width:260px;height:auto;max-height:76px;margin:0 auto 22px;object-fit:contain;object-position:center}'
        'h1{font-size:32px;line-height:1.2;margin:0 0 6px;letter-spacing:-.025em}'
        '.subtitle{font-size:15px;line-height:1.45;color:#68708f;margin:0 0 18px}'
        '.options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;align-items:stretch}'
        '.options.one-option{grid-template-columns:minmax(0,680px);justify-content:center}'
        '.card{display:flex;flex-direction:column;min-height:350px;background:#fff;border:1px solid #e1e4f1;border-radius:16px;padding:23px;box-shadow:0 5px 18px rgba(24,29,82,.045)}'
        '.options.one-option .card{min-height:0;padding:21px 24px}'
        '.card.primary{border:2px solid #6253d7;box-shadow:0 8px 24px rgba(73,61,171,.09)}'
        '.eyebrow{font-size:11px;font-weight:800;letter-spacing:.12em;color:#6556d9}'
        '.card h2{font-size:20px;margin:10px 0 12px;color:#171b4b}'
        '.percent{font-size:43px;font-weight:800;letter-spacing:-.04em;line-height:1;color:#211b65;margin:0 0 15px}'
        '.value-label{display:block;font-size:10px;font-weight:800;letter-spacing:.11em;color:#79809b;margin-bottom:6px}'
        '.price{font-size:22px;font-weight:800;color:#211b65;line-height:1.35}'
        '.copy{font-size:13px;line-height:1.5;color:#626b88;margin:12px 0 16px}'
        'form{margin:auto 0 0}.button{display:flex;align-items:center;justify-content:center;min-height:48px;width:100%;padding:12px 16px;border:0;border-radius:9px;background:#332a91;color:#fff;text-decoration:none;text-align:center;font-size:12px;font-weight:800;letter-spacing:.035em;cursor:pointer;transition:background-color 180ms ease,box-shadow 180ms ease,color 180ms ease,transform 180ms ease}'
        '.button-primary{box-shadow:0 3px 9px rgba(38,31,111,.13)}.button-primary:hover{background:#292176;box-shadow:0 6px 13px rgba(38,31,111,.18);transform:translateY(-1px)}.button-primary:active{transform:translateY(0);box-shadow:0 2px 5px rgba(38,31,111,.13)}'
        '.secondary .button{background:#fff;color:#342d85;border:1px solid #c8c5e8;box-shadow:0 1px 3px rgba(45,39,110,.04)}.secondary .button:hover{background:#f7f6ff;color:#28216f;border-color:#aaa4dc;box-shadow:0 2px 6px rgba(45,39,110,.07)}.secondary .button:active{background:#f1effb;transform:translateY(0)}'
        '.button:focus-visible,.link:focus-visible{outline:3px solid #8d82e8;outline-offset:3px}'
        '.link{display:inline-flex;align-items:center;gap:5px;padding:7px 9px;margin-left:-9px;border:1px solid transparent;border-radius:7px;color:#4134b2;text-decoration:none;font-weight:800;font-size:12px;letter-spacing:.035em;transition:background-color 180ms ease,border-color 180ms ease,color 180ms ease}'
        '.link:hover{background:#f8f7ff;border-color:#e7e4fb;color:#30258d}.link-arrow{display:inline-block;transition:transform 180ms ease}.link:hover .link-arrow{transform:translateX(2px)}'
        '.steps{margin-top:12px;padding:14px 20px;background:#f0f2fa;border-radius:14px}.steps h2{font-size:15px;margin:0 0 8px}'
        '.steps ol{margin:0;padding-left:21px;color:#59617d}.steps li{padding:4px 0 4px 4px;font-size:13px;line-height:1.5}'
        '.result{max-width:660px;margin:16px auto 0;padding:23px 26px;background:#fff;border:1px solid #e1e4f1;border-radius:16px;box-shadow:0 5px 18px rgba(24,29,82,.045)}'
        '.result h2{font-size:11px;letter-spacing:.12em;color:#6556d9;margin:0 0 8px}.result .percent{margin:0 0 25px}.result .price{margin:0 0 21px}'
        '.already{margin-top:18px;padding:15px;background:#f4f3fc;border-radius:10px;color:#4e5070;font-size:14px;line-height:1.6}'
        '@media(max-width:600px){.shell{padding:15px 14px 24px}.brand-logo{max-width:170px;max-height:48px;margin-bottom:12px}.brand{margin-bottom:12px}h1{font-size:24px;line-height:1.16}.subtitle{font-size:14px;line-height:1.4;margin-bottom:13px}.options,.options.one-option{grid-template-columns:1fr;gap:10px}.card,.options.one-option .card{min-height:0;padding:16px 17px;border-radius:13px}.eyebrow{font-size:10px}.card h2{font-size:18px;margin:7px 0 8px}.percent{font-size:38px;margin-bottom:10px}.value-label{font-size:9px;margin-bottom:4px}.price{font-size:20px}.copy{font-size:12px;line-height:1.42;margin:9px 0 10px}.card form{margin-top:9px}.button{min-height:48px;padding:10px 12px;font-size:11px}.steps{margin-top:9px;padding:12px 15px}.steps h2{font-size:14px;margin-bottom:5px}.steps li{padding:2px 0 2px 2px;font-size:12px;line-height:1.35}.result{margin-top:10px;padding:20px 17px}.result .percent{margin-bottom:15px}.result .price{margin-bottom:13px}}'
        '@media(prefers-reduced-motion:reduce){.button,.link,.link-arrow{transition:none}.button-primary:hover,.button-primary:active,.secondary .button:active,.link:hover .link-arrow{transform:none}}'
        f'</style></head><body><main class="shell"><header>{brand}'
        f'<h1>{escape(title)}</h1><p class="subtitle">{escape(subtitle)}</p></header>{content}'
        '</main></body></html>'
    )


def render_decision_page(
    *, recommended_pct: int, recommended_price: str, current_price: str,
    gradual_pct: int | None, gradual_price: str, recommended_url: str,
    gradual_url: str, advisor_url: str, logo_url: str = "", gradual_enabled: bool = False,
) -> str:
    gradual_available = gradual_enabled and gradual_pct is not None and int(gradual_pct) != int(recommended_pct)
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
        f'<form method="post" action="{escape(recommended_url, quote=True)}"><button class="button button-primary" type="submit">'
        'AUTORIZAR AJUSTE RECOMENDADO</button></form></section>'
    )
    if gradual_available:
        option = (
            '<section class="card secondary"><div class="eyebrow">OPCIÓN GRADUAL</div>'
            '<h2>Ajuste gradual</h2>'
            f'<p class="percent">{int(gradual_pct)}%</p>'
            '<span class="value-label">NUEVO VALOR SUGERIDO</span>'
            f'<div class="price">{gradual_price}</div>'
            f'<p class="copy">Si prefieres realizar un cambio más acotado, esta alternativa permite comenzar '
            f'con un ajuste inicial del {int(gradual_pct)}%. Así podremos observar la respuesta comercial '
            'de la propiedad antes de evaluar nuevos cambios de posicionamiento.</p>'
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
        f'<div class="options{"" if gradual_available else " one-option"}">{recommendation}{option if gradual_available else ""}</div>'
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
        subtitle = "Tu solicitud quedó registrada. Tu ejecutivo será informado para revisar la propuesta contigo."
        body = '<section class="result"><h2>SOLICITUD REGISTRADA</h2></section>'
    elif selected_type == "GRADUAL":
        title = "Ajuste gradual autorizado"
        subtitle = "Hemos registrado correctamente el ajuste gradual que seleccionaste."
        body = (
            '<section class="result"><h2>AJUSTE AUTORIZADO</h2>'
            f'<p class="percent">{int(selected_pct or 0)}%</p><h2>NUEVO VALOR AUTORIZADO</h2>'
            f'<div class="price">{selected_price}</div>'
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
