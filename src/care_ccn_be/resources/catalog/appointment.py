"""Catalog of bookable Care resources: one catalog per facility, one resource + offer per schedulable resource."""

from typing import Any

from care_ccn_be import adapter
from care_ccn_be.constants import (
    ACCEPTANCE_MODE_AUTO,
    DAY_OF_WEEK_NAMES,
    HEALTH_SERVICE_LAB_TEST,
    HEALTH_SERVICE_PHYSICAL_CONSULTATION,
)
from care_ccn_be.protocol.builder import CatalogBuilder
from care_ccn_be.resources.beckn import Catalog, Descriptor, Offer, Provider, Resource
from care_ccn_be.resources.beckn.codes import ClinicalDeliveryUnit, OfferType
from care_ccn_be.resources.beckn.health import (
    AvailabilitySlot,
    HealthOffer,
    HealthResource,
    OutcomeCommitment,
    Scheme,
)
from care_ccn_be.services import scheduling


def health_service_type(resource, schedules) -> str:
    """``Schedule.meta["beckn"]["healthServiceType"]`` wins, else it follows the resource kind."""
    override = scheduling.beckn_meta(schedules, "healthServiceType")
    if override:
        return override
    if scheduling.is_healthcare_service(resource):
        return HEALTH_SERVICE_LAB_TEST
    return HEALTH_SERVICE_PHYSICAL_CONSULTATION


def acceptance_mode(schedules) -> str:
    return scheduling.beckn_meta(schedules, "acceptanceMode") or ACCEPTANCE_MODE_AUTO


def availability_schedule(schedules) -> list[AvailabilitySlot]:
    entries = []
    for entry in scheduling.availability_entries(schedules):
        day = entry.get("day_of_week")
        if day is None or not 0 <= day < len(DAY_OF_WEEK_NAMES):
            continue
        entries.append(
            AvailabilitySlot(
                day_of_week=DAY_OF_WEEK_NAMES[day],
                start_time=str(entry.get("start_time", ""))[:5],
                end_time=str(entry.get("end_time", ""))[:5],
            )
        )
    return entries


def build_resource(resource, schedules) -> Resource:
    lab = scheduling.is_healthcare_service(resource)
    return Resource(
        id=str(resource.external_id),
        descriptor=Descriptor(name=scheduling.resource_name(resource)),
        resource_attributes=HealthResource(
            health_service_type=health_service_type(resource, schedules),
            acceptance_mode=acceptance_mode(schedules),
            availability_schedule=availability_schedule(schedules),
            clinical_delivery_unit=ClinicalDeliveryUnit.TEST if lab else None,
            accepted_schemes=[Scheme(code="PMJAY")] if lab else None,
        ),
    )


def build_offer(resource, schedules) -> Offer:
    lab = scheduling.is_healthcare_service(resource)
    outcome = OutcomeCommitment(
        attendance_to_outcome_sla_hours=24, outcome_note_format_standard="FHIR/DiagnosticReport", mandatory=True
    )
    return Offer(
        id=f"offer-{resource.external_id}",
        resource_ids=[str(resource.external_id)],
        descriptor=Descriptor(name=f"Appointment - {scheduling.resource_name(resource)}"),
        offer_attributes=HealthOffer(
            health_service_type=health_service_type(resource, schedules),
            offer_type=OfferType.SINGLE_EVENT,
            outcome_commitment=outcome if lab else None,
        ),
    )


class AppointmentCatalogBuilder(CatalogBuilder):
    def catalogs(self, spec: Any = None) -> list[dict[str, Any]]:
        public_only = getattr(spec, "public_only", True)
        bap_endpoint = adapter.bap_endpoint()
        bpp_endpoint = adapter.bpp_endpoint()
        result = []
        for bucket in scheduling.schedules_by_facility(public_only):
            facility = bucket["facility"]
            all_schedules = [s for item in bucket["resources"] for s in item["schedules"]]
            catalog = Catalog(
                id=f"catalog-{facility.external_id}",
                descriptor=Descriptor(name=facility.name),
                provider=Provider(id=str(facility.external_id), descriptor=Descriptor(name=facility.name)),
                # a provider may point consumers at its own BPP; otherwise Care is the BPP of its catalog
                bpp_id=scheduling.beckn_meta(all_schedules, "bppId") or bpp_endpoint.id or None,
                bpp_uri=scheduling.beckn_meta(all_schedules, "bppUri") or bpp_endpoint.uri or None,
                bap_id=bap_endpoint.id or None,
                bap_uri=bap_endpoint.uri or None,
                resources=[build_resource(item["resource"], item["schedules"]) for item in bucket["resources"]],
                offers=[build_offer(item["resource"], item["schedules"]) for item in bucket["resources"]],
            )
            result.append(catalog.dump())
        return result

    def build(self, spec: Any = None) -> dict[str, Any]:
        """The first catalog; callers that need all of them use ``catalogs``."""
        catalogs = self.catalogs(spec)
        return catalogs[0] if catalogs else {}
