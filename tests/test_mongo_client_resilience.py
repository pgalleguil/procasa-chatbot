"""Defensive limits must apply identically to PyMongo and Motor pools."""
import motor.motor_asyncio

from chatbot import storage


def test_sync_and_async_mongo_clients_have_bounded_cross_region_timeouts(monkeypatch):
    created = []

    class Client:
        def __init__(self, uri, **kwargs):
            created.append(kwargs)

        def __getitem__(self, database):
            return {"database": database}

    monkeypatch.setattr(storage, "_mongo_client", None)
    monkeypatch.setattr(storage, "_async_mongo_client", None)
    monkeypatch.setattr(storage, "MongoClient", Client)
    monkeypatch.setattr(motor.motor_asyncio, "AsyncIOMotorClient", Client)
    monkeypatch.setattr(storage, "_patch_mongo_forensics", lambda: None)

    storage.get_db()
    storage.get_async_db()

    assert len(created) == 2
    for options in created:
        assert options["socketTimeoutMS"] == 20000
        assert options["connectTimeoutMS"] == 10000
        assert options["serverSelectionTimeoutMS"] == 15000
        assert options["maxPoolSize"] == 25
        assert options["waitQueueTimeoutMS"] == 15000
        assert options["maxIdleTimeMS"] == 45000
