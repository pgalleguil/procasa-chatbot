"""Recover ONE pending commercial event after a published MLC -> Prop360 match.

Read-only by default. Example:
 python scripts/recuperar_lead_portal_mlc.py \
   --phone +569XXXXXXXX --mlc MLC-1234567890 --property-code 12345 --env-file C:/secrets/.env
To write, add --execute AFTER independently verifying the publisher's property
code and active PROCASA SUCRE ownership. Never creates a new WhatsApp message.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


def _args():
    parser = argparse.ArgumentParser(description="Resuelve una publicación MLC y reanuda el evento original (sin WhatsApp al cliente).")
    parser.add_argument("--phone", required=True)
    parser.add_argument("--mlc", required=True, help="MLC-<digits>, verified from the customer's URL")
    parser.add_argument("--property-code", required=True, help="Código interno publicado por la corredora en Portal Inmobiliario")
    parser.add_argument("--env-file", default="", help="Ruta .env con MONGO_URI / DB_NAME")
    parser.add_argument("--execute", action="store_true", help="Persiste mapeo verificado y reanuda el evento")
    return parser.parse_args()


def main() -> int:
    args = _args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    if args.env_file:
        from dotenv import load_dotenv
        if not Path(args.env_file).is_file():
            print("ERROR: --env-file no existe", file=sys.stderr)
            return 2
        load_dotenv(args.env_file, override=True)

    from chatbot.storage import get_db
    from chatbot.property_lookup import PROPERTY_COLLECTION_NAME, property_availability_state
    from chatbot.portal_verified_reference import (
        COLLECTION, publication_identity, persist_verified_portal_link,
    )
    from chatbot.inventory_reconciliation import resume_waiting_event

    phone = args.phone.strip()
    mlc = args.mlc.upper().replace("_", "-")
    code = args.property_code.strip()
    if not re.fullmatch(r"\+?569\d{8}", phone) or not re.fullmatch(r"MLC-\d{8,13}", mlc):
        print("ERROR: teléfono chileno o identificador MLC inválido", file=sys.stderr)
        return 2
    if not re.fullmatch(r"\d{4,7}", code):
        print("ERROR: código Prop360 inválido", file=sys.stderr)
        return 2

    db = get_db()
    lead_docs = list(db["leads"].find({"phone": phone}).limit(2))
    if len(lead_docs) != 1:
        print("ERROR: se esperaba exactamente un lead canónico", file=sys.stderr)
        return 3
    lead = lead_docs[0]
    props = list(db[PROPERTY_COLLECTION_NAME].find({"codigo": code}).limit(2))
    if len(props) != 1:
        print("ERROR: el código no existe de forma única en Prop360", file=sys.stderr)
        return 3
    prop = props[0]
    office = str((prop.get("estado") or {}).get("oficina") or (prop.get("resumen") or {}).get("oficina") or "")
    if "PROCASA SUCRE" not in office.upper() or property_availability_state(prop) != "PROPERTY_FOUND_AVAILABLE":
        print("ERROR: la ficha no es una propiedad activa de PROCASA SUCRE", file=sys.stderr)
        return 3
    events = list(db["commercial_processing_events"].find({
        "lead_id": lead["_id"], "phone": phone,
        "source_external_id": mlc, "commercial_processing_state": "waiting_inventory_sync",
    }).limit(2))
    if len(events) != 1:
        print("ERROR: se esperaba un solo evento comercial pendiente con este MLC", file=sys.stderr)
        return 3
    event = events[0]
    url = str(event.get("source_property_code") or "")
    identity = publication_identity(url)
    if not identity or identity[0] != mlc or urlsplit(url).hostname is None:
        print("ERROR: URL del evento no coincide con el MLC y dominio autorizado", file=sys.stderr)
        return 3
    if "arriendo" in url.casefold() and (prop.get("tipo_operacion") or {}).get("arriendo") is not True:
        print("ERROR: la operación de Prop360 no coincide con el enlace", file=sys.stderr)
        return 3
    if "venta" in url.casefold() and (prop.get("tipo_operacion") or {}).get("venta") is not True:
        print("ERROR: la operación de Prop360 no coincide con el enlace", file=sys.stderr)
        return 3
    if lead.get("assignment_type") not in {"MISSING_PROPERTY", "AMBIGUOUS_PROPERTY_REFERENCE"}:
        print("ERROR: este lead ya no requiere recuperación de propiedad", file=sys.stderr)
        return 3
    from chatbot.crm_metrics import active_assignment_cycle
    if active_assignment_cycle(db, lead["_id"]):
        print("ERROR: el lead tiene un ciclo activo; no se reasigna automáticamente", file=sys.stderr)
        return 3
    mapping_id = f"portal_inmobiliario:{mlc}"
    old_mapping = db[COLLECTION].find_one({"_id": mapping_id})
    if old_mapping and old_mapping.get("property_code") != code:
        print("ERROR: existe un mapeo verificado conflictivo", file=sys.stderr)
        return 3

    report = {
        "lead_id": str(lead["_id"]), "phone": phone, "mlc": mlc,
        "property_code": code, "owner_in_prop360": (prop.get("estado") or {}).get("ejecutivo"),
        "event_id": str(event["_id"]), "event_state": event["commercial_processing_state"],
        "preview": not args.execute,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.execute:
        print("SOLO DIAGNÓSTICO: no se ha escrito ni asignado ningún lead.")
        print("Para aplicar: confirma el código en el portal y agrega --execute.")
        return 0

    if not persist_verified_portal_link(db, url, code, "operator_verified_public_portal"):
        print("ERROR: no se pudo persistir el mapeo, no se ejecuta el evento", file=sys.stderr)
        return 4
    outcome = resume_waiting_event(db, event)
    fresh = db["leads"].find_one({"_id": lead["_id"]}) or {}
    print(json.dumps({
        "recovery": outcome,
        "lead_assignment": fresh.get("ejecutivo_asignado"),
        "property_code": (fresh.get("prospecto") or {}).get("codigo"),
        "commercial_processing_state": fresh.get("commercial_processing_state"),
    }, ensure_ascii=False, default=str, indent=2))
    return 0 if outcome.get("status") == "completed" and (fresh.get("prospecto") or {}).get("codigo") == code else 5


if __name__ == "__main__":
    raise SystemExit(main())
