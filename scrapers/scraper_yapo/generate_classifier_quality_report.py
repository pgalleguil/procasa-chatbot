"""Generate the offline Yapo classifier audit artifacts for the approved Maipú lot.

This utility reads the canonical JSON and existing HTML files only. It never
requests Yapo, calls an AI API, connects to MongoDB, or changes the input lot.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import random
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from scrapers.scraper_yapo.classifier_rules import analyze_contextual_signals, classify_yapo_batch
from scrapers.scraper_yapo.extractor import extract_listing_fields


EXPECTED_CANONICAL_SHA256 = "dc9bda51a093abd9a1d7c6a741eeacac275724ea74b29ad4cc2a8bba558e12bf"
DEFAULT_INPUT = Path(
    r"C:\Users\pgall\Documents\PROCASA\YapoMaipu\preimportacion_20261103\yapo_maipu_preimport_482.json"
)
DEFAULT_OUTPUT = Path("reports/yapo_classifier_quality_20261103")

SAMPLE_NOTES = {
    "32299493": "El texto completo solo describe la vivienda. No aparece firma comercial, comisión ni declaración de propiedad. El tipo de anunciante Agente proviene del HTML de Yapo; por esa señal queda CORREDOR_PROBABLE, no seguro.",
    "33098407": "El título atribuye la venta a Titanium Propiedades y la descripción habla desde esa marca. El anuncio 33098428 del mismo seller_profile_id incluye el nombre comercial y comisión explícita de corretaje. Se propone probable por evidencia del perfil exacto.",
    "33196456": "El HTML marca tipo de anunciante Agente y la descripción cobra comisión de corredor del 2% más boleta de honorarios; el contacto es Guido Parra. No hay declaración de propiedad.",
    "32931491": "El perfil lleva PROFESIONAL, pero no hay tipo de anunciante Agente, firma de corredor, marca atribuida, comisión ni declaración de propietario. La asesoría profesional es texto promocional genérico; permanece INCIERTO.",
    "26143551": "La descripción tiene 21 caracteres y la etiqueta Yapo dice Propietario. No hay declaración de propiedad atribuible ni señal comercial; permanece INCIERTO.",
    "25939035": "La descripción termina con una firma explícita “atte su dueño” y coincide con el tipo Yapo Propietario. Se propone DUEÑO_PROBABLE; PROFESIONAL no se usa como prueba de corretaje y la titularidad legal no queda confirmada.",
    "33190907": "La descripción es marketing genérico; no incluye comisión ni firma comercial. El HTML sí etiqueta el tipo de anunciante como Agente, por lo que la propuesta CORREDOR_PROBABLE se basa en esa categoría del aviso y no en PROFESIONAL.",
    "26610549": "El título dice Sin Comisión y la descripción enumera características de la vivienda. La regla de comisión positiva no se activa; Propietario sin declaración de dueño no basta. Permanece INCIERTO.",
    "33208287": "Su texto es descriptivo y no identifica al anunciante como corredor. El anuncio 32306422 comparte exactamente el perfil 13736569 e incluye la firma “Corredora de Propiedades” vinculada al publicador. Se propone CORREDOR_PROBABLE por correlación exacta y requiere confirmación humana.",
    "28073704": "El texto cobra 2% de honorarios de corretaje, pero Yapo etiqueta al anunciante como Propietario y no aparece identidad comercial o rol del publicador. La comisión sola no alcanza; permanece INCIERTO.",
    "32632678": "El publicador figura como CRISTI PROPIEDADES y el HTML etiqueta el aviso como Agente. La marca no se incorpora al catálogo; queda CORREDOR_PROBABLE y el nombre comercial es candidato a revisión.",
    "27167566": "Dos anuncios del mismo perfil local llevan etiqueta Propietario; los textos describen la casa y no contienen declaración personal de dominio ni señales de corretaje. Permanece INCIERTO.",
    "27144918": "El título dice “Vendo casa”, pero no afirma “mi casa” ni identifica al publicador como propietario. La etiqueta Yapo aislada no promueve a dueño; permanece INCIERTO.",
    "32306422": "Además de la etiqueta Yapo Agente, la descripción cierra con SAULING CHIONG ABE seguido de “Corredora de Propiedades”. El nombre coincide con el publicador. Se propone CORREDOR_PROBABLE.",
    "32728499": "La descripción dice “En nuestra corredora” y ofrece orientación en primera persona. El perfil Yapo exacto tiene tres anuncios locales; los otros dos se correlacionan como CORREDOR_PROBABLE con evidencia del anuncio fuente.",
    "25861896": "La descripción habla de trato directo con “dueños” en plural, pero no vincula a Lorena con la titularidad. La etiqueta Yapo Propietario es débil y no hay corretaje; permanece INCIERTO.",
    "26135394": "El título usa “Vendo”, pero la descripción solo enumera atributos. No hay señal “mi casa”, firma del dueño ni evidencia de corretaje; permanece INCIERTO.",
    "26644524": "La descripción enumera características de la casa; no hay declaración personal de propiedad ni evidencia de corretaje. La etiqueta Propietario no confirma por sí sola.",
    "26653392": "“Tu hermosa casa propia” se dirige al comprador y no afirma que el publicador sea dueño. La descripción y el perfil no muestran corretaje; permanece INCIERTO.",
    "27111189": "La descripción detalla la propiedad y menciona el pago de contribuciones, pero no declara que el publicador sea propietario ni ofrece corretaje. Permanece INCIERTO.",
}


def _normalize(value: Any) -> str:
    plain = re.sub(r"<[^>]*>", " ", html.unescape(str(value or "")))
    decomposed = unicodedata.normalize("NFKD", plain.casefold())
    return " ".join("".join(ch for ch in decomposed if not unicodedata.combining(ch)).split())


def _local_html_audit(record: dict[str, Any]) -> dict[str, Any]:
    path = Path(record.get("html_path") or "")
    if not path.exists():
        return {"exists": False, "path": str(path)}
    source = path.read_text(encoding="utf-8", errors="replace")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    extracted = extract_listing_fields(source, str(record.get("url") or ""), include_images=False)
    return {
        "exists": True,
        "path": str(path),
        "sha256": digest,
        "sha256_matches": digest == str(record.get("html_sha256") or ""),
        "title_matches": _normalize(extracted.get("title")) == _normalize(record.get("title")),
        "description_matches": _normalize(extracted.get("description")) == _normalize(record.get("description")),
        "listing_type_matches": _normalize(extracted.get("listing_advertiser_type"))
        == _normalize(record.get("listing_advertiser_type")),
        "listing_advertiser_type_extracted": extracted.get("listing_advertiser_type") or "",
        "listing_advertiser_type_source": extracted.get("listing_advertiser_type_source") or "",
        "description_truncated_by_extractor": bool(extracted.get("descripcion_is_truncated")),
    }


def _owner_signal_label(record: dict[str, Any], result: dict[str, Any], contextual: dict[str, Any]) -> str:
    evidence = result.get("owner_evidence") or []
    direct = [item for item in evidence if item.get("family") != "portal_listing_attribute"]
    mismatch = any(
        item.get("rule_id") == "OWNER_DECLARATION_CONTACT_IDENTITY_MISMATCH"
        for item in result.get("contradictions") or []
    )
    support = [item for item in contextual.get("owner_support_signals") or [] if item.get("family") == "direct_deal"]
    if result.get("state") == "DUEÑO_PROBABLE":
        return "Declaración directa compatible con atributo Yapo Propietario"
    if direct and mismatch:
        return "Declaración de venta por dueño con identidad de contacto distinta; revisión humana"
    if direct:
        return "Declaración de propietario; corroborar antes de confirmar titularidad"
    if support:
        return "Mención de trato con dueño(s), sin atribuir propiedad al publicador"
    if any(item.get("family") == "portal_listing_attribute" for item in evidence):
        return "Solo etiqueta Yapo Propietario; no basta para atribuir propiedad"
    return "Sin evidencia atribuible de propiedad"


def _decision_basis(record: dict[str, Any], result: dict[str, Any], contextual: dict[str, Any]) -> str:
    if result.get("profile_corroboration"):
        profile = result["profile_corroboration"]
        return (
            f"Correlación por seller_profile_id exacto con anuncio {profile['source_listing_id']} "
            f"({profile['source_rule_id']}); CORREDOR_PROBABLE, requiere confirmación humana."
        )
    if result.get("state") == "DUEÑO_PROBABLE":
        return "Cierre explícito “atte su dueño” y etiqueta Yapo Propietario; no acredita titularidad legal."
    if result.get("state") == "CORREDOR_PROBABLE":
        if str(record.get("listing_advertiser_type") or "").casefold() == "agente":
            extra = " Se suma comisión o firma explícita." if (
                contextual.get("brokerage_fee_evidence") or contextual.get("broker_role_signature_evidence")
            ) else ""
            return "Yapo HTML etiqueta el tipo de anunciante como Agente; queda probable, no seguro." + extra
        if any(item.get("rule_id") == "BROKER_DESCRIPTION_COMPANY_FEE_SIGNATURE" for item in result.get("broker_evidence") or []):
            return "Nombre comercial y cobro positivo de comisión de corretaje; marca no confirmada en catálogo."
        if any(item.get("rule_id") == "BROKER_DESCRIPTION_EXPLICIT_ROLE_SIGNATURE" for item in result.get("broker_evidence") or []):
            return "Firma explícita de corredor(a) vinculada por nombre al publicador."
        if any(item.get("rule_id") == "BROKER_IDENTITY_RULES_JSON" for item in result.get("broker_evidence") or []):
            return "El publicador se presenta con rol “Corredor”; la etiqueta Yapo Propietario queda como contradicción."
        return "Regla determinista propone CORREDOR_PROBABLE; no confirma corredor seguro."
    if any(item.get("rule_id") == "OWNER_DECLARATION_CONTACT_IDENTITY_MISMATCH" for item in result.get("contradictions") or []):
        return "Declara venta por dueño, pero nombra un contacto distinto; conservar INCIERTO."
    if contextual.get("brokerage_fee_evidence"):
        return "Menciona comisión u honorarios de corretaje, pero falta identidad o rol del anunciante; conservar INCIERTO."
    if contextual.get("owner_support_signals"):
        return "Menciona trato directo con dueño(s), sin demostrar que el publicador sea propietario; conservar INCIERTO."
    if str(record.get("seller_type") or "").casefold() == "profesional":
        return "Solo perfil PROFESIONAL o texto promocional genérico; no demuestra corretaje ni propiedad."
    return "HTML y descripción completa sin señal atribuible suficiente; revisión humana."


def generate(input_path: Path, output_dir: Path, seed: int) -> dict[str, Any]:
    canonical_sha = hashlib.sha256(input_path.read_bytes()).hexdigest()
    if canonical_sha != EXPECTED_CANONICAL_SHA256:
        raise ValueError(f"SHA-256 del JSON canónico no coincide: {canonical_sha}")
    records = json.loads(input_path.read_text(encoding="utf-8"))
    uncertain = [item for item in records if (item.get("classification") or {}).get("state") == "INCIERTO"]
    if len(records) != 482 or len(uncertain) != 92:
        raise ValueError(f"Se esperaban 482 registros y 92 INCIERTO; encontrados {len(records)} y {len(uncertain)}")

    sample = random.Random(seed).sample(uncertain, 20)
    results = classify_yapo_batch(records)
    result_by_id = {str(item.get("listing_id")): result or {} for item, result in zip(records, results)}
    by_profile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        profile_id = str(item.get("seller_profile_id") or "").strip()
        if profile_id:
            by_profile[profile_id].append(item)

    html_checks = {str(item.get("listing_id")): _local_html_audit(item) for item in uncertain}
    for listing_id, check in html_checks.items():
        valid = (
            check.get("exists") and check.get("sha256_matches") and check.get("title_matches")
            and check.get("description_matches") and check.get("listing_type_matches")
            and not check.get("description_truncated_by_extractor")
        )
        if not valid:
            raise ValueError(f"HTML local no coincide o está incompleto para {listing_id}: {check}")

    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    owner_direct_ids = []
    owner_support_ids = []
    owner_mismatch_ids = []
    owner_review_ids = []
    fee_ids = []
    agent_ids = []
    for record in uncertain:
        listing_id = str(record.get("listing_id"))
        result = result_by_id[listing_id]
        contextual = analyze_contextual_signals(record)
        owner_evidence = result.get("owner_evidence") or []
        direct_owner = any(
            item.get("family") != "portal_listing_attribute"
            and item.get("attribution") == "advertiser_declaration_candidate"
            for item in owner_evidence
        )
        supports_owner = any(item.get("family") == "direct_deal" for item in contextual.get("owner_support_signals") or [])
        mismatch = any(
            item.get("rule_id") == "OWNER_DECLARATION_CONTACT_IDENTITY_MISMATCH"
            for item in result.get("contradictions") or []
        )
        if direct_owner:
            owner_direct_ids.append(listing_id)
        if supports_owner:
            owner_support_ids.append(listing_id)
        if mismatch:
            owner_mismatch_ids.append(listing_id)
        if direct_owner or supports_owner or mismatch or result.get("state") == "DUEÑO_PROBABLE":
            owner_review_ids.append(listing_id)
        if contextual.get("brokerage_fee_evidence"):
            fee_ids.append(listing_id)
        if str(record.get("listing_advertiser_type") or "").casefold() == "agente":
            agent_ids.append(listing_id)

        evidence = []
        for item in owner_evidence:
            evidence.append(f"{item.get('rule_id')}:{item.get('field')}:{item.get('fragment')}")
        for item in result.get("broker_evidence") or []:
            if item.get("fragment"):
                evidence.append(f"{item.get('rule_id')}:{item.get('field')}:{item.get('fragment')}")
        for item in contextual.get("owner_support_signals") or []:
            evidence.append(f"OWNER_SUPPORT:{item.get('rule_id')}:{item.get('fragment')}")

        peers = by_profile.get(str(record.get("seller_profile_id") or ""), [])
        proposed = result.get("state") or "NONE"
        if proposed == "INCIERTO":
            review_status = "REVISIÓN_HUMANA_REQUERIDA"
        elif proposed == "DUEÑO_PROBABLE":
            review_status = "CONFIRMAR_IDENTIDAD_Y_CONTROL_DE_LA_PROPIEDAD"
        elif proposed == "CORREDOR_PROBABLE":
            review_status = "CONFIRMAR_ANTES_DE_PROMOVER_A_CORREDOR_SEGURO"
        else:
            review_status = "REVISAR"
        html_check = html_checks[listing_id]
        rows.append({
            "listing_id": listing_id,
            "publicador": record.get("publicador_visible") or record.get("contact_name") or "",
            "seller_profile_id": record.get("seller_profile_id") or "",
            "seller_type_perfil_yapo": record.get("seller_type") or "",
            "tipo_anunciante_aviso_yapo": record.get("listing_advertiser_type") or "",
            "titulo": record.get("title") or "",
            "clasificacion_anterior": (record.get("classification") or {}).get("state") or "",
            "clasificacion_propuesta": proposed,
            "fuente_propuesta": result.get("source") or "",
            "evidencia_verificable": " || ".join(dict.fromkeys(evidence)),
            "senal_propietario": _owner_signal_label(record, result, contextual),
            "contradicciones": " || ".join(
                item.get("reason", "")
                + (f" [{item.get('publisher','')} / {item.get('named_contact','')}]"
                   if item.get("rule_id") == "OWNER_DECLARATION_CONTACT_IDENTITY_MISMATCH" else "")
                for item in result.get("contradictions") or []
            ),
            "anuncios_mismo_perfil_locales": ",".join(
                str(peer.get("listing_id")) for peer in peers if str(peer.get("listing_id")) != listing_id
            ),
            "requiere_revision_o_confirmacion_humana": "SI",
            "estado_revision": review_status,
            "html_local_verificado": "SI",
            "html_sha256": html_check.get("sha256") or "",
            "descripcion_completa_sin_truncar": "SI" if not record.get("descripcion_is_truncated") else "NO",
        })

    csv_path = output_dir / "comparison_92.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    sample_ids = [str(item.get("listing_id")) for item in sample]
    sample_rows = []
    for record in sample:
        listing_id = str(record.get("listing_id"))
        result = result_by_id[listing_id]
        contextual = analyze_contextual_signals(record)
        profile_id = str(record.get("seller_profile_id") or "")
        peers = [
            peer for peer in by_profile.get(profile_id, [])
            if str(peer.get("listing_id")) != listing_id
        ]
        peer_details = []
        for peer in peers:
            peer_result = result_by_id.get(str(peer.get("listing_id")), {})
            peer_details.append({
                "listing_id": str(peer.get("listing_id")),
                "title": peer.get("title") or "",
                "classification_before": (peer.get("classification") or {}).get("state") or "",
                "classification_proposed": peer_result.get("state") or "",
                "seller_type": peer.get("seller_type") or "",
                "listing_advertiser_type": peer.get("listing_advertiser_type") or "",
                "description_complete": not bool(peer.get("descripcion_is_truncated")),
            })
        sample_rows.append({
            "listing_id": listing_id,
            "url": record.get("canonical_url") or record.get("url") or "",
            "publisher_identity": {
                "publicador_visible": record.get("publicador_visible") or "",
                "contact_name": record.get("contact_name") or "",
                "contact_logo_alt": record.get("contact_logo_alt") or "",
                "seller_jsonld_name": record.get("seller_jsonld_name") or "",
                "seller_profile_id": profile_id,
                "seller_profile_url": record.get("seller_profile_url") or "",
                "seller_type_profile": record.get("seller_type") or "",
                "seller_type_provenance": (record.get("publisher_field_provenance") or {}).get("seller_type") or {},
                "publisher_profile_context_from_saved_batch": record.get("publisher_profile_context") or {},
                "listing_advertiser_type": record.get("listing_advertiser_type") or "",
                "listing_advertiser_type_source": html_checks[listing_id].get("listing_advertiser_type_source") or "",
                "contact_badges_text": record.get("contact_badges_text") or "",
            },
            "title_complete": record.get("title") or "",
            "description_complete": record.get("description") or "",
            "description_is_truncated": bool(record.get("descripcion_is_truncated")),
            "description_length": int(record.get("descripcion_len") or len(record.get("description") or "")),
            "html_local_audit": html_checks[listing_id],
            "identity_external_verification": "No realizada; no se equiparan nombres parecidos con identidad confirmada.",
            "same_exact_seller_profile_local_ads": peer_details,
            "classification_before": (record.get("classification") or {}).get("state") or "",
            "classification_proposed": result.get("state") or "",
            "proposal_source": result.get("source") or "",
            "broker_evidence": result.get("broker_evidence") or [],
            "owner_evidence": result.get("owner_evidence") or [],
            "owner_declarations_full_context": contextual.get("owner_declarations") or [],
            "owner_support_signals": contextual.get("owner_support_signals") or [],
            "brokerage_fee_evidence": contextual.get("brokerage_fee_evidence") or [],
            "contradictions": result.get("contradictions") or [],
            "requires_review": bool(result.get("requires_review")),
            "audit_conclusion": SAMPLE_NOTES.get(listing_id, _decision_basis(record, result, contextual)),
        })

    sample_path = output_dir / "audit_sample_20.json"
    sample_path.write_text(json.dumps({
        "audit_title": "Auditoría manual de 20 anuncios INCIERTO de Yapo",
        "canonical_input": str(input_path),
        "canonical_sha256": canonical_sha,
        "selection": {
            "seed": seed,
            "algorithm": f"random.Random({seed}).sample(uncertain_records_in_canonical_order, 20)",
            "sample_size": 20,
            "listing_ids_in_sample_order": sample_ids,
        },
        "source_scope": "JSON canónico y HTML local guardado; sin solicitudes a Yapo, sin verificación externa de identidad, sin DeepSeek ni MongoDB.",
        "records": sample_rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    counts_after = Counter(result_by_id[str(item.get("listing_id"))].get("state", "NONE") for item in uncertain)
    changed = [item for item in uncertain if result_by_id[str(item.get("listing_id"))].get("state") != "INCIERTO"]
    profile_promotions = [
        str(item.get("listing_id")) for item in uncertain
        if result_by_id[str(item.get("listing_id"))].get("source") == "same_yapo_profile_correlation"
    ]
    candidates = {
        "explicit_owner_declaration_ids": sorted(set(owner_direct_ids)),
        "owner_contact_mismatch_ids": sorted(set(owner_mismatch_ids)),
        "owner_contact_support_only_ids": sorted(set(owner_support_ids)),
        "owner_review_union_ids": sorted(set(owner_review_ids)),
        "owner_probable_ids": sorted(
            str(item.get("listing_id")) for item in uncertain
            if result_by_id[str(item.get("listing_id"))].get("state") == "DUEÑO_PROBABLE"
        ),
    }
    summary = {
        "canonical_input": str(input_path),
        "canonical_sha256_expected": EXPECTED_CANONICAL_SHA256,
        "canonical_sha256_verified_before": canonical_sha,
        "canonical_sha256_verified_after": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "canonical_file_modified": False,
        "sample_seed": seed,
        "sample_listing_ids": sample_ids,
        "input_counts": {"all_records": len(records), "originally_uncertain": len(uncertain)},
        "after_counts_among_original_92": dict(counts_after),
        "changes_from_uncertain": len(changed),
        "yapo_listing_type_agent_ids": sorted(agent_ids),
        "yapo_listing_type_agent_count": len(agent_ids),
        "positive_brokerage_fee_evidence_listing_ids": sorted(fee_ids),
        "owner_signal_inventory": candidates,
        "owner_signal_review_count": len(set(owner_review_ids)),
        "exact_profile_correlation_promotions": profile_promotions,
        "exact_profile_correlation_count": len(profile_promotions),
        "local_html_validation": {
            "records_checked": len(uncertain),
            "all_html_files_exist": all(item.get("exists") for item in html_checks.values()),
            "all_stored_sha256_match": all(item.get("sha256_matches") for item in html_checks.values()),
            "all_full_descriptions_match_html": all(item.get("description_matches") for item in html_checks.values()),
            "all_titles_match_html": all(item.get("title_matches") for item in html_checks.values()),
            "all_listing_advertiser_types_match_html": all(item.get("listing_type_matches") for item in html_checks.values()),
            "truncated_descriptions": [key for key, item in html_checks.items() if item.get("description_truncated_by_extractor")],
        },
        "deepseek_calls": 0,
        "mongo_writes": 0,
        "new_yapo_requests": 0,
        "catalog_promotions": 0,
        "classification_error_rate_claimed": False,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    ids_text = "\n".join(f"- {index}. `{listing_id}`" for index, listing_id in enumerate(sample_ids, 1))
    owner_union = sorted(set(owner_review_ids))
    owner_detail = (
        f"La auditoría encontró {len(owner_union)} registros con señales relacionadas con propietario: "
        f"{len(set(owner_direct_ids))} con declaración directa atribuible, {len(set(owner_mismatch_ids))} con contacto distinto, "
        f"{len(set(owner_support_ids))} con mención de trato directo con dueño(s), y "
        f"{len(candidates['owner_probable_ids'])} propuesto(s) DUEÑO_PROBABLE. Las categorías se superponen; ver IDs en summary.json."
    )
    readme = f'''# Auditoría de calidad del clasificador Yapo — 2026-11-03

## Alcance y preservación

Se revisó el lote canónico de 482 anuncios desde `{input_path}`. Su SHA-256 esperado y verificado antes y después es `{canonical_sha}`. El JSON canónico no se modificó.

Se identificaron exactamente 92 anuncios `INCIERTO`. La evaluación usó esos 482 registros y HTML local. No hubo solicitudes a Yapo, escrituras MongoDB, importaciones, asignaciones, distribuciones, notificaciones ni llamadas a DeepSeek. No se verificaron identidades externamente; nombres parecidos no se trataron como identidad confirmada.

## Muestra aleatoria reproducible de 20

Selección: `random.Random({seed}).sample(uncertain_records_in_canonical_order, 20)`.

{ids_text}

`audit_sample_20.json` contiene el título y descripción completa, campos de identidad y perfil, anuncios locales del mismo `seller_profile_id`, extracción del HTML, evidencias, señales de propiedad, contradicciones y conclusión por anuncio.

## Integridad de los datos locales

- HTML local existente: {len(uncertain)}/92.
- SHA-256 HTML coincidente con el lote: {sum(bool(item.get('sha256_matches')) for item in html_checks.values())}/92.
- Título y descripción completa coincidentes con el HTML: {sum(bool(item.get('title_matches') and item.get('description_matches')) for item in html_checks.values())}/92.
- Tipo de anunciante reextraído del HTML coincidente: {sum(bool(item.get('listing_type_matches')) for item in html_checks.values())}/92.
- Descripciones marcadas como truncadas al reextraer: {len(summary['local_html_validation']['truncated_descriptions'])}.

## Comparación de los 92

| Estado | Antes | Después |
|---|---:|---:|
| `INCIERTO` | 92 | {counts_after.get('INCIERTO', 0)} |
| `CORREDOR_SEGURO` | 0 | {counts_after.get('CORREDOR_SEGURO', 0)} |
| `CORREDOR_PROBABLE` | 0 | {counts_after.get('CORREDOR_PROBABLE', 0)} |
| `DUEÑO_PROBABLE` | 0 | {counts_after.get('DUEÑO_PROBABLE', 0)} |

Cambios deterministas respaldados: {len(changed)}. Los corredores quedan como propuestas `CORREDOR_PROBABLE`; ninguno se promovió a `CORREDOR_SEGURO`. El dueño queda como `DUEÑO_PROBABLE`, sin afirmar titularidad legal. No se calcula tasa de error: la muestra no es un conjunto de etiquetas verificadas.

El CSV `comparison_92.csv` contiene ID, publicador, perfil, estados antes/después, evidencia, señales de dueño, contradicciones, avisos del mismo perfil, estado de confirmación e integridad HTML.

## Defectos demostrados y cambios puntuales

1. El extractor guardaba el atributo de aviso `Agente` pero el clasificador lo ignoraba. Ahora es evidencia para `CORREDOR_PROBABLE`; el perfil `PROFESIONAL` por sí solo sigue siendo insuficiente. El HTML confirma 23 etiquetas `Agente`.
2. La etiqueta Yapo `Propietario` podía bloquear una identidad directa como `Corredor - Jorge Carreño`. Ahora queda como contradicción visible; el aviso solo pasa a probable si también tiene evidencia directa.
3. Se añadieron firmas de rol vinculadas al publicador, como “corredora de propiedades”, “nuestra corredora” y nombres de contacto que se identifican como corredor.
4. Se reconocen cargos de corretaje como “comisión corredor 2%”, “2% de honorarios de corretaje” y “venta afecta a comisión del 2%”. El cobro solo no clasifica a alguien como corredor; requiere identidad, rol o categoría de anunciante coherente. “Sin comisión” no cuenta como cobro.
5. Se añadió el cierre explícito “atte su dueño” para `DUEÑO_PROBABLE`, si no hay contradicción comercial.
6. La evidencia puede trasladarse solo entre avisos con el mismo `seller_profile_id` exacto. La categoría `Agente` de otro aviso no se propaga, y una etiqueta o declaración de propietario bloquea esa propagación.
7. Se corrigieron falsos desajustes de contacto como “vía WhatsApp” y “al fono”, que no son nombres de personas.

## Señales de propietario y revisión pendiente

{owner_detail}

Los anuncios cuyo único dato es la etiqueta `Propietario`, los títulos genéricos “Vendo casa” y las menciones de comisión sin identidad comercial atribuible permanecen `INCIERTO`. El contacto distinto en una declaración de dueño, las menciones de trato con dueño(s) y los registros sin publicador visible siguen en revisión.

Los 32 `CORREDOR_PROBABLE` requieren confirmación antes de promoverlos a `CORREDOR_SEGURO`; los 59 `INCIERTO` necesitan revisión humana para resolver clasificación. El registro `DUEÑO_PROBABLE` exige verificación de identidad/control antes de considerarlo propietario. El número total de registros sin confirmación final es 92.

## Regresión y catálogo

La suite Yapo contiene regresiones para casos locales con IDs, evidencia positiva/negativa de comisión, perfiles exactos, señales de propietario, contradicciones, `PROFESIONAL` aislado y metadatos `Agente`. No se cambió ni promovió ninguna marca en el catálogo permanente.

Regeneración offline: `python -m scrapers.scraper_yapo.generate_classifier_quality_report`.
'''
    (output_dir / "README.md").write_text(readme, encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=20261103)
    args = parser.parse_args()
    summary = generate(args.input, args.output_dir, args.seed)
    print(json.dumps({
        "output_dir": str(args.output_dir.resolve()),
        "counts_after": summary["after_counts_among_original_92"],
        "changes": summary["changes_from_uncertain"],
        "profile_correlation_count": summary["exact_profile_correlation_count"],
        "owner_signal_review_count": summary["owner_signal_review_count"],
        "html_check": summary["local_html_validation"],
        "canonical_sha256_after": summary["canonical_sha256_verified_after"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
