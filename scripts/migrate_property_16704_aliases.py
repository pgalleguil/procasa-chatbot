"""Idempotent alias migration for Prop360 property 16704.

Dry-run is the default. Use --apply only after reviewing the printed diff.
"""
from __future__ import annotations
import argparse, copy, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import datetime, timezone
from config import Config
from pymongo import MongoClient
from chatbot.property_lookup import build_property_alias, merge_property_aliases

URLS = [
    ("mercadolibre", "arriendo", "https://casa.mercadolibre.cl/MLC-4247982034-casa-en-arriendo-de-3-dorm-en-puente-alto-_JM", "MLC-4247982034"),
    ("portal_inmobiliario", "arriendo", "https://www.portalinmobiliario.com/MLC-4247982034-casa-en-arriendo-de-3-dorm-en-puente-alto-_JM", "MLC-4247982034"),
    ("toctoc", "arriendo", "https://www.toctoc.com/propiedades/arriendocorredorasr/casas/puente-alto/casa-en-arriendo-en-puente-alto/b004575345d086712b8514b7a0c52970f0930059", "b004575345d086712b8514b7a0c52970f0930059"),
    ("yapo", "arriendo", "https://www.yapo.cl/bienes-raices-alquiler-casas/casa-en-arriendo-en-puente-alto/32757789", "32757789"),
]

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args=ap.parse_args()
    client=MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=10000)
    col=client[Config.DB_NAME][Config.PROPERTY_COLLECTION_NAME]
    docs=list(col.find({"codigo":{"$in":["16704",16704]}}))
    if len(docs)!=1:
        raise SystemExit(f"expected exactly one canonical property 16704, found {len(docs)}")
    doc=docs[0]
    aliases=[build_property_alias(url, portal, operation, external_id) for portal,operation,url,external_id in URLS]
    merged=merge_property_aliases((doc.get("publicaciones") or {}).get("aliases", []), aliases)
    before=copy.deepcopy(doc)
    after=copy.deepcopy(doc)
    after.setdefault("publicaciones", {})["aliases"]=merged
    # Prop360 explicitly has simultaneous rental publication; keep historical fields
    # and only enable the operation flag, without touching versions/audit hashes.
    after.setdefault("tipo_operacion", {})["arriendo"]=True
    diff={"before_aliases": (doc.get("publicaciones") or {}).get("aliases", []),
          "after_aliases": merged,
          "before_arriendo": (doc.get("tipo_operacion") or {}).get("arriendo"),
          "after_arriendo": after["tipo_operacion"].get("arriendo"),
          "canonical_id": str(doc.get("_id")), "codigo": str(doc.get("codigo"))}
    print(json.dumps(diff, ensure_ascii=False, indent=2, default=str))
    if not args.apply:
        print("DRY_RUN_ONLY")
        return
    backup_dir=Path("backups"); backup_dir.mkdir(exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir.joinpath(f"property_16704_before_aliases_{stamp}.json").write_text(
        json.dumps(before, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    result=col.update_one(
        {"_id": doc["_id"], "codigo": doc["codigo"],
         "audit_hash": doc.get("audit_hash"), "versiones": doc.get("versiones")},
        {"$set":{"publicaciones.aliases": merged, "tipo_operacion.arriendo": True}},
    )
    if result.matched_count != 1:
        raise SystemExit("guard failed: precondition mismatch; no document changed")
    print(json.dumps({"applied":True,"modified_count":result.modified_count,
                      "backup":str(backup_dir/f"property_16704_before_aliases_{stamp}.json")}))
if __name__=="__main__":
    main()
