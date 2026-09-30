from campanas import owner_campaign_live_sender as sender


def _rows():
    return [{"property_code": code} for code in ("1001", "1002", "1003")]


def test_send_batch_stops_after_failed_and_leaves_later_rows_unprocessed(monkeypatch):
    called = []

    def fake_send(_db, row, *_args, **_kwargs):
        called.append(row["property_code"])
        return {"property_code": row["property_code"], "smtp_status": "FAILED"}

    results, reason, unsent = sender._send_batch(
        None, _rows(), "campaign", "batch", send_one=fake_send,
    )

    assert called == ["1001"]
    assert results == [{"property_code": "1001", "smtp_status": "FAILED"}]
    assert reason == "FAILED"
    assert unsent == 2


def test_send_batch_stops_after_delivery_unknown_without_retry(monkeypatch):
    called = []

    def fake_send(_db, row, *_args, **_kwargs):
        called.append(row["property_code"])
        return {"property_code": row["property_code"], "smtp_status": "DELIVERY_UNKNOWN"}

    results, reason, unsent = sender._send_batch(
        None, _rows(), "campaign", "batch", send_one=fake_send,
    )

    assert called == ["1001"]
    assert results[0]["smtp_status"] == "DELIVERY_UNKNOWN"
    assert reason == "DELIVERY_UNKNOWN"
    assert unsent == 2


def test_send_batch_stops_after_unhandled_exception_without_touching_later_rows():
    called = []

    def fake_send(_db, row, *_args, **_kwargs):
        called.append(row["property_code"])
        raise sender.SenderError("document_routing_error")

    results, reason, unsent = sender._send_batch(
        None, _rows(), "campaign", "batch", send_one=fake_send,
    )

    assert called == ["1001"]
    assert results[0]["smtp_status"] == "UNHANDLED_EXCEPTION"
    assert results[0]["error_code"] == "document_routing_error"
    assert reason == "document_routing_error"
    assert unsent == 2


def test_send_batch_continues_only_when_each_row_is_sent():
    called = []

    def fake_send(_db, row, *_args, **_kwargs):
        called.append(row["property_code"])
        return {"property_code": row["property_code"], "smtp_status": "SENT"}

    results, reason, unsent = sender._send_batch(
        None, _rows(), "campaign", "batch", send_one=fake_send,
    )

    assert called == ["1001", "1002", "1003"]
    assert [item["smtp_status"] for item in results] == ["SENT", "SENT", "SENT"]
    assert reason is None
    assert unsent == 0
