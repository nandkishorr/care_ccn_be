"""The referral catalog: one coordinator catalog per facility, ``catalog-coordinator-<facility id>``.

It always carries the facility's Care Coordination Desk (resource + offer) and, next to it, one resource + offer per
public, active availability the facility (or one of its organizations) receives network referrals on. The catalog
id never changes, so a new availability is merged into the facility's catalog already on the network. The forms are
not published here: the BPP returns them on ``on_select`` of an offer."""

from typing import Any

from django.db.models import Q

from care_ccn_be import adapter
from care_ccn_be.protocol.builder import CatalogBuilder
from care_ccn_be.resources.beckn import Catalog, Descriptor, Offer, Provider, Resource
from care_ccn_be.resources.beckn.codes import CoordinationAcceptanceMode, UrgencyTier
from care_ccn_be.resources.beckn.health import (
    CatalogReferralAvailability,
    CoordinationScope,
    CoordinationSlaWindow,
    ReferralCategory,
    Scheme,
    ServiceCoordinationOffer,
    ServiceCoordinationResource,
    TargetServiceType,
)
from care_ccn_be.settings import plugin_settings

CLINICAL_REFERRAL = ("CLINICAL", "Clinical referral")
TARGET_SERVICE_FOR_TYPE = {"consultation": "CONSULTATION", "lab": "INVESTIGATION"}


def catalog_id(facility) -> str:
    return f"catalog-coordinator-{facility.external_id}"


def coordinator_resource_id(facility) -> str:
    return f"coordinator-{facility.external_id}-resource"


def coordinator_offer_id(facility) -> str:
    return f"coordinator-{facility.external_id}-offer"


def offer_id(availability) -> str:
    return f"offer-{availability.external_id}"


def coordination_scope(target_service_types: list[str]) -> CoordinationScope:
    code, display = CLINICAL_REFERRAL
    return CoordinationScope(
        referral_categories=[ReferralCategory(code=code, display=display)],
        target_service_types=[TargetServiceType(code=item) for item in target_service_types],
        programme_scope=[Scheme(code=item) for item in plugin_settings.BECKN_COORDINATOR_PROGRAMMES] or None,
    )


def coordination_resource(target_service_types: list[str], **extra) -> ServiceCoordinationResource:
    return ServiceCoordinationResource(
        coordination_scope=coordination_scope(target_service_types),
        acceptance_mode=CoordinationAcceptanceMode.MANUAL_REVIEW,
        supported_urgency_tiers=list(UrgencyTier),
        **extra,
    )


def coordination_offer() -> ServiceCoordinationOffer:
    hours = plugin_settings.BECKN_COORDINATOR_SLA_HOURS or {}
    windows = [
        CoordinationSlaWindow(
            clinical_urgency_tier=tier, lapse_window_hours=hours[tier.value], sla_breach_window_hours=hours[tier.value]
        )
        for tier in UrgencyTier
        if hours.get(tier.value) is not None
    ]
    return ServiceCoordinationOffer(
        coordination_sla_windows=windows or None,
        retry_policy=plugin_settings.BECKN_COORDINATOR_RETRY_POLICY or None,
        handoff_supported=plugin_settings.BECKN_COORDINATOR_HANDOFF_SUPPORTED,
        max_active_referrals=plugin_settings.BECKN_COORDINATOR_MAX_ACTIVE_REFERRALS or None,
    )


def build_coordinator_resource(facility) -> Resource:
    return Resource(
        id=coordinator_resource_id(facility),
        descriptor=Descriptor(name=f"{plugin_settings.BECKN_COORDINATOR_NAME} Coordination"),
        resource_attributes=coordination_resource(list(plugin_settings.BECKN_COORDINATOR_TARGET_SERVICE_TYPES)),
    )


def build_coordinator_offer(facility) -> Offer:
    return Offer(
        id=coordinator_offer_id(facility),
        resource_ids=[coordinator_resource_id(facility)],
        descriptor=Descriptor(name=plugin_settings.BECKN_COORDINATOR_NAME),
        offer_attributes=coordination_offer(),
    )


def build_resource(availability) -> Resource:
    organization = availability.internal_organization
    target = TARGET_SERVICE_FOR_TYPE.get(availability.referral_type, availability.referral_type.upper())
    return Resource(
        id=str(availability.external_id),
        descriptor=Descriptor(name=availability.name, short_desc=availability.description or None),
        resource_attributes=coordination_resource(
            [target],
            referral_availability=CatalogReferralAvailability(
                id=str(availability.external_id),
                referral_type=availability.referral_type,
                entity_type=availability.entity_type,
                organization_id=str(organization.external_id) if organization else None,
                organization_name=organization.name if organization else None,
            ),
        ),
    )


def build_offer(availability) -> Offer:
    return Offer(
        id=offer_id(availability),
        resource_ids=[str(availability.external_id)],
        descriptor=Descriptor(name=availability.name),
        offer_attributes=coordination_offer(),
    )


def build_catalog(facility, items: list) -> dict[str, Any]:
    bap_endpoint = adapter.bap_endpoint()
    bpp_endpoint = adapter.bpp_endpoint()
    return Catalog(
        id=catalog_id(facility),
        descriptor=Descriptor(name=plugin_settings.BECKN_COORDINATOR_NAME),
        provider=Provider(id=str(facility.external_id), descriptor=Descriptor(name=facility.name)),
        bpp_id=bpp_endpoint.id or None,
        bpp_uri=bpp_endpoint.uri or None,
        bap_id=bap_endpoint.id or None,
        bap_uri=bap_endpoint.uri or None,
        resources=[build_coordinator_resource(facility), *(build_resource(item) for item in items)],
        offers=[build_coordinator_offer(facility), *(build_offer(item) for item in items)],
    ).dump()


class ReferralCatalogBuilder(CatalogBuilder):
    def catalogs(self, spec: Any = None) -> list[dict[str, Any]]:
        """Every facility with a receiving availability; with ``spec.facility_ids`` exactly those facilities (a
        facility left without one keeps its catalog, holding only the coordination desk)."""
        from care.emr.resources.referral.common import ReferralEntityType
        from care.facility.models import Facility

        from care_ccn_be.services import availability as availabilities

        public_only = getattr(spec, "public_only", True)
        facility_ids = getattr(spec, "facility_ids", None)
        queryset = availabilities.receiving(public_only)
        buckets: dict[int, tuple[Any, list]] = {}
        if facility_ids is not None:
            queryset = queryset.filter(
                Q(entity_type=ReferralEntityType.facility.value, facility_id__in=facility_ids)
                | Q(
                    entity_type=ReferralEntityType.internal_organization.value,
                    internal_organization__facility_id__in=facility_ids,
                )
            )
            for facility in Facility.objects.filter(pk__in=facility_ids, deleted=False).order_by("pk"):
                buckets[facility.pk] = (facility, [])
        for availability in queryset.order_by("pk"):
            facility = availabilities.facility_of(availability)
            if facility is None or getattr(facility, "deleted", False):
                continue
            buckets.setdefault(facility.pk, (facility, []))[1].append(availability)
        return [build_catalog(facility, items) for facility, items in buckets.values()]

    def build(self, spec: Any = None) -> dict[str, Any]:
        """The first catalog; callers that need all of them use ``catalogs``."""
        catalogs = self.catalogs(spec)
        return catalogs[0] if catalogs else {}
