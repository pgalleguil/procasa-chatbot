"""Internal, read-only owner portal prototype."""

from .service import (
    OFFICE_SCOPE,
    SUCRE_OFFICE_FIELD,
    SUCRE_OFFICE_VALUE,
    build_owner_portal_view,
    get_owner_portal_property_view,
    is_procasa_sucre_property,
    select_preview_property_code,
)
from .schemas import (
    MarketIndicatorV1,
    OwnerPortalAccessV1,
    OwnerPortalEventV1,
    OwnerPortalMarketContextV1,
    OwnerPortalPositioningV1,
    OwnerPortalPropertyViewV1,
)

__all__ = [
    "OFFICE_SCOPE",
    "SUCRE_OFFICE_FIELD",
    "SUCRE_OFFICE_VALUE",
    "build_owner_portal_view",
    "get_owner_portal_property_view",
    "is_procasa_sucre_property",
    "select_preview_property_code",
    "OwnerPortalAccessV1",
    "OwnerPortalEventV1",
    "OwnerPortalPropertyViewV1",
    "MarketIndicatorV1",
    "OwnerPortalMarketContextV1",
    "OwnerPortalPositioningV1",
]
