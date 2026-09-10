"""Internal, read-only owner portal prototype."""

from .service import (
    OFFICE_SCOPE,
    SUCRE_OFFICE_FIELD,
    SUCRE_OFFICE_VALUE,
    build_owner_portal_view,
    is_procasa_sucre_property,
    select_preview_property_code,
)

__all__ = [
    "OFFICE_SCOPE",
    "SUCRE_OFFICE_FIELD",
    "SUCRE_OFFICE_VALUE",
    "build_owner_portal_view",
    "is_procasa_sucre_property",
    "select_preview_property_code",
]
