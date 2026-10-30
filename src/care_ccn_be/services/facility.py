"""Facility lookups by the external ids carried in Beckn payloads."""

import uuid
from collections.abc import Iterable

from rest_framework.exceptions import NotFound


def by_external_id(external_id):
    """A non-UUID id belongs to a facility outside this Care instance and can never match."""
    from care.facility.models import Facility

    if not external_id:
        return None
    try:
        uuid.UUID(str(external_id))
    except (ValueError, TypeError):
        return None
    return Facility.objects.filter(external_id=external_id).first()


def resolve(candidates: Iterable) -> object | None:
    for candidate in candidates:
        facility = by_external_id(candidate)
        if facility is not None:
            return facility
    return None


def require(external_id):
    facility = by_external_id(external_id)
    if facility is None:
        raise NotFound("Unknown facility")
    return facility


def default_geo_organization(facility):
    return getattr(facility, "geo_organization", None)
