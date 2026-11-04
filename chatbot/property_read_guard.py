"""Read-minimized Prop360 access for customer-facing chatbot lookups.

This scope is intentionally used only by chatbot lookup and recommendation
paths. CRM, executive routing, and operations continue to use their original
database objects. It does not modify source records.
"""
from __future__ import annotations

from copy import deepcopy

from config import Config

# Inclusion projection: never return unrestricted property documents to a
# customer-facing conversational model. Do not include the full ubicacion,
# propietario, contacto, metadata, publicaciones, observaciones or descripcion.
_SAFE_FIELDS = frozenset({
    "codigo", "codigo_prop360", "codigo_internacional",
    "disponible_prop360", "disponible", "status",
    "estado.disponible_prop360", "estado.disponible", "estado.estado_prop360",
    "estado.oficina", "estado.status",
    "tipo_operacion.tipo", "tipo_operacion.venta", "tipo_operacion.arriendo",
    "tipo_operacion.precio_venta.precio_uf",
    "tipo_operacion.precio_venta.precio_clp",
    "tipo_operacion.precio_arriendo.precio_clp",
    "tipo_operacion.precio_arriendo.precio_uf",
    "tipo_operacion.gastos_comunes",
    "ubicacion.comuna", "ubicacion.region",
    "caracteristicas.dormitorios", "caracteristicas.banos",
    "caracteristicas.bodegas", "caracteristicas.estacionamientos",
    "caracteristicas.estacionamientos_cubiertos",
    "caracteristicas.estacionamientos_descubiertos",
    "caracteristicas.orientacion", "caracteristicas.superficie_util",
    "caracteristicas.superficie_total", "caracteristicas.superficie_terraza",
    "caracteristicas.superficie_terreno",
    "caracteristicas.superficie_construida",
    "caracteristicas.piso", "caracteristicas.numero_pisos",
    "caracteristicas.ano_construccion", "caracteristicas.features",
    "caracteristicas.ascensores",
    "comuna", "region", "oficina_nombre", "tipo", "operacion",
    "precio_uf", "precio_clp", "dormitorios", "banos",
    "m2_utiles", "m2_totales", "estacionamientos", "bodega",
    # Internal numeric vector supports RAG ranking only; RAG removes it before
    # returning chat text. It contains no recoverable literal street field.
    "vector_descripcion",
})
SAFE_PROPERTY_PROJECTION = {name: 1 for name in sorted(_SAFE_FIELDS)}
SAFE_PROPERTY_PROJECTION["_id"] = 0

_PROPERTY_COLLECTIONS = frozenset({
    "universo_cartera", "universo_cartera_prop360",
    Config.PROPERTY_COLLECTION_NAME, Config.COLLECTION_NAME,
})


def _safe_scalar(value):
    return (
        isinstance(value, (str, int, float, bool))
        and (not isinstance(value, str) or len(value) <= 10000)
    )


def _filter_property(doc: dict | None) -> dict | None:
    """Defense in depth if a mock/adapter ignores MongoDB projection."""
    if not isinstance(doc, dict):
        return None
    filtered: dict = {}
    for path in _SAFE_FIELDS:
        current = doc
        parts = path.split(".")
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                break
            current = current[part]
        else:
            if path == "vector_descripcion":
                if (not isinstance(current, list) or len(current) > 4096
                        or not all(isinstance(x, (int, float)) for x in current)):
                    continue
            elif path == "caracteristicas.features":
                if (not isinstance(current, list) or len(current) > 200
                        or not all(isinstance(x, str) and len(x) <= 100
                                   for x in current)):
                    continue
            elif not _safe_scalar(current):
                continue
            target = filtered
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = deepcopy(current)
    return filtered


class _SafeCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def sort(self, *args, **kwargs):
        self._cursor = self._cursor.sort(*args, **kwargs)
        return self

    def limit(self, *args, **kwargs):
        self._cursor = self._cursor.limit(*args, **kwargs)
        return self

    def __iter__(self):
        for doc in self._cursor:
            safe = _filter_property(doc)
            if safe is not None:
                yield safe


class _SafePropertyCollection:
    def __init__(self, collection):
        self._collection = collection

    def find_one(self, filter, projection=None, *args, **kwargs):
        doc = self._collection.find_one(
            filter, SAFE_PROPERTY_PROJECTION, *args, **kwargs
        )
        return _filter_property(doc)

    def find(self, filter=None, projection=None, *args, **kwargs):
        cursor = self._collection.find(
            {} if filter is None else filter,
            SAFE_PROPERTY_PROJECTION, *args, **kwargs
        )
        return _SafeCursor(cursor)

    def __getattr__(self, name):
        # No aggregate, distinct, update, raw collection or arbitrary read
        # method can bypass this customer-facing property projection.
        raise PermissionError(f"Operation not allowed on chatbot property view: {name}")


class _SafePropertyDatabase:
    def __init__(self, db):
        self._db = db

    def __getitem__(self, name):
        collection = self._db[name]
        if name in _PROPERTY_COLLECTIONS:
            return _SafePropertyCollection(collection)
        return collection

    def get_collection(self, name, *args, **kwargs):
        if name in _PROPERTY_COLLECTIONS:
            return self[name]
        return self._db.get_collection(name, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._db, name)


def chatbot_safe_property_db(db):
    """Idempotent scope: property documents are read with safe projection."""
    if isinstance(db, _SafePropertyDatabase):
        return db
    return _SafePropertyDatabase(db)
