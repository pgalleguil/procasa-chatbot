"""Offline read-only pilot; never sends messages or modifies the CRM.

From repository root (MONGO_URI configured):
    python -m scripts.commercial_recommendations_dry_run --limit-leads 100
    python -m scripts.commercial_recommendations_dry_run --limit-leads 100 --neighbors --output /tmp/commercial-recs.json
"""
import argparse
import hashlib
import json

from chatbot.commercial_recommendations import (
    DEFAULT_OFFICE, preview_for_lead, source_code_from_lead,
)
from chatbot.property_lookup import PROPERTY_COLLECTION_NAME
from chatbot.storage import get_db

PROPERTY_FIELDS = {
    "_id": 0, "codigo": 1, "disponible_prop360": 1,
    "estado.oficina": 1, "estado.disponible_prop360": 1,
    "resumen.oficina": 1, "resumen.precio_uf": 1,
    "tipo_operacion": 1, "ubicacion.comuna": 1, "caracteristicas": 1,
    "observaciones.descripcion": 1, "observaciones.titulo": 1,
    "vector_descripcion": 1, "publicaciones.procasa.url_procasa": 1,
}
LEAD_FIELDS = {
    "_id": 1, "codigo_propiedad": 1, "source_property_code": 1,
    "codigo": 1, "cartera_data.canonical_code": 1,
    "prospecto.codigo": 1, "prospecto.search_criteria": 1,
    "prospecto.rag_search_state.criteria": 1,
    "search_criteria": 1, "rag_search_state.criteria": 1,
    "pipeline_stage": 1, "stage": 1, "propiedades_vistas": 1,
    "opt_out": 1, "do_not_contact": 1, "unsubscribed": 1,
    "no_contact": 1,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit-leads", type=int, default=100)
    parser.add_argument("--office", default=DEFAULT_OFFICE)
    parser.add_argument("--network", action="store_true",
                        help="Include all PROCASA offices (requires commercial approval)")
    parser.add_argument("--neighbors", action="store_true",
                        help="Allow neighboring communes when same commune has few results")
    parser.add_argument("--output", default=None, help="Path for PII-free JSON report")
    args = parser.parse_args()
    if not 1 <= args.limit_leads <= 1000:
        parser.error("--limit-leads must be between 1 and 1000")

    db = get_db()
    inventory_query = {"disponible_prop360": True}
    if not args.network:
        inventory_query["estado.oficina"] = args.office
    # Single bounded snapshot of inventory and one lead batch.
    inventory = list(db[PROPERTY_COLLECTION_NAME].find(inventory_query, PROPERTY_FIELDS))
    leads = list(db["leads"].find({}, LEAD_FIELDS).sort("_id", -1).limit(args.limit_leads))
    codes = sorted({code for lead in leads if (code := source_code_from_lead(lead))})
    origins = db[PROPERTY_COLLECTION_NAME].find({"codigo": {"$in": codes}}, PROPERTY_FIELDS)
    sources = {str(prop["codigo"]): prop for prop in origins}

    results = []
    for lead in leads:
        preview = preview_for_lead(
            lead, sources, inventory, office=None if args.network else args.office,
            allow_neighbors=args.neighbors,
        )
        preview["lead_ref"] = hashlib.sha256(
            str(lead.get("_id") or "").encode("utf-8")
        ).hexdigest()[:12]
        results.append(preview)

    counts = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    report = {
        "mode": "READ_ONLY", "send_enabled": False,
        "inventory_active": len(inventory), "leads_reviewed": len(results),
        "status_counts": counts, "results": results,
    }
    if args.output:
        with open(args.output, "w", encoding="utf-8") as out:
            json.dump(report, out, ensure_ascii=False, indent=2)
        print("Reporte comercial generado sin nombres ni telefonos:", args.output)
    else:
        print(json.dumps({k: v for k, v in report.items() if k != "results"},
                         ensure_ascii=False, indent=2))
        print("Use --output para inspeccionar los codigos recomendados y sus puntajes.")


if __name__ == "__main__":
    main()
