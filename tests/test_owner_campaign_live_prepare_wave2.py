import csv

from campanas import owner_campaign_live_prepare as prepare
from campanas import owner_campaign_test_runtime as runtime
from campanas.owner_campaign_live_config import WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID


def test_wave2_selection_is_code_scoped_and_allows_repeated_owner_email(tmp_path, monkeypatch):
    codes = ["6581", "6583"]
    owner = "shared.owner@example.com"
    masters = [
        {
            "codigo": code,
            "estado": {"oficina": "PROCASA SUCRE", "estado_prop360": "Activa", "ejecutivo": "Exec"},
            "disponible_prop360": True,
        }
        for code in codes
    ]

    class Properties:
        query = None

        def find(self, query):
            self.query = query
            return iter(masters)

    class DB:
        def __init__(self):
            self.properties = Properties()

        def __getitem__(self, name):
            if name == runtime.PROPERTY_COLLECTION:
                return self.properties
            raise AssertionError(f"unexpected collection access: {name}")

    monkeypatch.setattr(runtime, "_is_sucre", lambda _master: True)
    monkeypatch.setattr(runtime, "_active_available", lambda _master: True)
    monkeypatch.setattr(runtime, "_email_from_property", lambda _master, required=True: owner)
    monkeypatch.setattr(runtime, "calculate_owner_campaign_lead_percentiles", lambda *_args, **_kwargs: {})

    def fake_row_for(_db, master, _percentiles, _now):
        return {"property_code": master["codigo"], "owner_email": owner, "send_status": "READY"}

    monkeypatch.setattr(prepare, "_row_for", fake_row_for)
    selection = tmp_path / "selection.csv"
    with selection.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["batch_id", "campaign_id", "property_code"])
        writer.writeheader()
        writer.writerows({"batch_id": "wave2_batch_01", "campaign_id": WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID, "property_code": code} for code in codes)

    db = DB()
    output = tmp_path / "prepared.csv"
    result = prepare.prepare_manifest_from_selection(selection, output, db=db)

    assert result["manifest_count"] == 2
    assert db.properties.query["codigo"]["$in"] == [variant for code in codes for variant in runtime._variants(code)]
    with output.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["property_code"] for row in rows] == codes
    assert {row["owner_email"] for row in rows} == {owner}
    assert {row["campaign_id"] for row in rows} == {WAVE2_SINGLE_PROPERTY_CAMPAIGN_ID}
    assert {row["send_status"] for row in rows} == {"READY"}
