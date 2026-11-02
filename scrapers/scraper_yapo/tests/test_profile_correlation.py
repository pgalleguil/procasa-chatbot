from types import SimpleNamespace

from mongo_store import MongoStore


class FakeCollection:
    def __init__(self, docs): self.docs = docs
    def find(self, *_args, **_kwargs): return self.docs


class FakeStore(MongoStore):
    def __init__(self, docs):
        super().__init__(SimpleNamespace())
        self._docs = docs
    def collection(self): return FakeCollection(self._docs)


def test_count_alone_never_confirms_commercial_profile():
    store = FakeStore([
        {"listing_id": str(i), "classification": {"state": "INCIERTO"}}
        for i in range(20)
    ])
    ctx = store.publisher_profile_context({"seller_profile_id": "p1"})
    assert ctx["linked_publications"] == 20
    assert ctx["commercial_identity_confirmed"] is False


def test_confirmed_broker_plus_identity_confirms_profile():
    store = FakeStore([
        {"listing_id": "1", "classification": {"state": "CORREDOR_SEGURO"},
         "company_name": "Corredores del Maule"},
    ])
    ctx = store.publisher_profile_context({"seller_profile_id": "p1"})
    assert ctx["commercial_identity_confirmed"] is True
    assert ctx["confirmed_broker_count"] == 1
