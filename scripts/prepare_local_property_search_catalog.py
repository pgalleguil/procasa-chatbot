"""Create a sanitized local candidate snapshot from a property-only JSON export."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chatbot.local_property_search import ATTRIBUTES, number, public_text, public_url, public_value
from scripts.validate_local_property_search_catalog import SAFE_FIELDS

TEXT_FIELDS = {"source_portal", "operation", "property_type", "commune", "region", "sector",
               "neighborhood", "neighbourhood", "locality", "title", "description", "search_text"}


def prepare_catalog(source: Path, target: Path):
    if source.resolve() == target.resolve():
        raise ValueError("source and output must be different files")
    rows = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("input must be a non-empty JSON array")
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("every catalog item must be an object")
        item = {}
        for key in SAFE_FIELDS:
            if key not in row:
                continue
            value = row[key]
            if key in TEXT_FIELDS:
                value = public_text(value)
            elif key == "canonical_url":
                value = public_url(value)
            elif key == "attributes":
                source_attributes = value
                value = {name: number(attribute) for name, attribute in source_attributes.items()
                         if name in ATTRIBUTES and number(attribute) is not None} if isinstance(source_attributes, dict) else {}
            elif key == "features":
                value = public_value(value)
            elif key == "geo":
                value = {name: part for name, part in value.items()
                         if name in {"latitude", "longitude", "radius_m", "verified"}
                         and (isinstance(part, bool) if name == "verified" else
                              isinstance(part, (int, float)) and not isinstance(part, bool) and math.isfinite(part))} if isinstance(value, dict) else {}
            if value is not None and value != "" and value != {}:
                item[key] = value
        clean.append(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(clean, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return len(clean)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path,
                        help="property-only JSON export; never pass a full MongoDB document dump")
    parser.add_argument("--output", required=True, type=Path,
                        help="new candidate path; existing files are never overwritten")
    args = parser.parse_args()
    print(f"candidate_rows={prepare_catalog(args.input, args.output)} output={args.output}")


if __name__ == "__main__":
    main()
