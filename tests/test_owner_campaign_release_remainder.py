from copy import deepcopy

from campanas import owner_campaign_release_remainder as release


CAMPAIGN = "owner_price_sucre_wave1_20260928"
PILOT_CODES = {"16469", "16479", "16486", "16492", "16520", "16521", "16523", "16527", "16533", "16548"}


class _UpdateResult:
    def __init__(self, modified_count):
        self.modified_count = modified_count


class _Ledger:
    def __init__(self, records):
        self.records = records

    def find(self, query):
        return [deepcopy(row) for row in self.records if row.get("campaign_id") == query["campaign_id"]]

    def update_many(self, query, update):
        modified = 0
        for row in self.records:
            if all(row.get(key) == value for key, value in query.items()):
                row.update(update["$set"])
                modified += 1
        return _UpdateResult(modified)


class _DB:
    def __init__(self, records):
        self.ledger = _Ledger(records)

    def __getitem__(self, _name):
        return self.ledger


def _records():
    pilot = []
    for index, code in enumerate(sorted(PILOT_CODES)):
        owner = f"owner{index}@example.com"
        boss = "boss@example.com"
        executive = f"exec{index}@example.com"
        pilot.append({
            "_id": f"{CAMPAIGN}:{code}", "campaign_id": CAMPAIGN,
            "campaign_stage": "PILOT", "batch_id": release.PILOT_BATCH_ID,
            "property_code": code, "owner_email": owner, "boss_cc": boss,
            "executive_email": executive, "send_status": "SENT",
            "send_attempts": [{
                "smtp_status": "SENT", "message_id": f"<m{index}@example.com>",
                "owner_email": owner, "final_cc_list": [boss, executive],
                "batch_id": release.PILOT_BATCH_ID,
            }],
        })
    remainder = []
    ordinal = 0
    for batch_id, count in release.EXPECTED_BATCH_SIZES.items():
        for _ in range(count):
            code = str(20000 + ordinal)
            remainder.append({
                "_id": f"{CAMPAIGN}:{code}", "campaign_id": CAMPAIGN,
                "campaign_stage": "REMAINDER", "batch_id": batch_id,
                "property_code": code, "send_status": "READY_PENDING_PILOT",
            })
            ordinal += 1
    return pilot + remainder


def test_a_valid_pilot_releases_exactly_270_pending_rows():
    db = _DB(_records())
    result = release.execute_release(db, CAMPAIGN, PILOT_CODES, execute=True)
    assert result["allowed"] is True
    assert result["modified_count"] == 270
    assert sum(row["send_status"] == "READY" for row in db.ledger.records if row["campaign_stage"] == "REMAINDER") == 270


def test_b_nine_of_ten_sent_denies_release():
    rows = _records()
    rows[0]["send_status"] = "READY"
    assert release.evaluate_release(CAMPAIGN, PILOT_CODES, rows)["allowed"] is False


def test_c_failed_pilot_denies_release():
    rows = _records()
    rows[0]["send_status"] = "FAILED"
    rows[0]["send_attempts"][0]["smtp_status"] = "FAILED"
    assert release.evaluate_release(CAMPAIGN, PILOT_CODES, rows)["allowed"] is False


def test_d_delivery_unknown_pilot_denies_release():
    rows = _records()
    rows[0]["send_status"] = "DELIVERY_UNKNOWN"
    rows[0]["send_attempts"][0]["smtp_status"] = "DELIVERY_UNKNOWN"
    assert release.evaluate_release(CAMPAIGN, PILOT_CODES, rows)["allowed"] is False


def test_e_second_release_is_idempotent_and_changes_zero_rows():
    db = _DB(_records())
    first = release.execute_release(db, CAMPAIGN, PILOT_CODES, execute=True)
    second = release.execute_release(db, CAMPAIGN, PILOT_CODES, execute=True)
    assert first["modified_count"] == 270
    assert second["allowed"] is True
    assert second["modified_count"] == 0


def test_f_remainder_already_sent_is_never_reset_or_released():
    rows = _records()
    target = next(row for row in rows if row["campaign_stage"] == "REMAINDER")
    target["send_status"] = "SENT"
    db = _DB(rows)
    result = release.execute_release(db, CAMPAIGN, PILOT_CODES, execute=True)
    assert result["allowed"] is False
    assert result["modified_count"] == 0
    assert target["send_status"] == "SENT"
    assert sum(row["send_status"] == "READY" for row in db.ledger.records if row["campaign_stage"] == "REMAINDER") == 0
