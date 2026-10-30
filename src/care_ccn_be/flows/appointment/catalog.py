"""Catalog of bookable Care resources: one catalog per facility, one resource + offer per schedulable resource."""

from typing import Any

from care_ccn_be.flows.appointment.constants import (
    ACCEPTANCE_MODE_AUTO,
    CODED_VALUE_CONTEXT,
    DAY_OF_WEEK_NAMES,
    HEALTH_OFFER_CONTEXT,
    HEALTH_RESOURCE_CONTEXT,
    HEALTH_SERVICE_LAB_TEST,
    HEALTH_SERVICE_PHYSICAL_CONSULTATION,
)
from care_ccn_be.protocol.builder import CatalogBuilder
from care_ccn_be.services import scheduling
from care_ccn_be.settings import plugin_settings


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


def availability_schedule(schedules) -> list[dict[str, str]]:
    entries = []
    for entry in scheduling.availability_entries(schedules):
        day = entry.get("day_of_week")
        if day is None or not 0 <= day < len(DAY_OF_WEEK_NAMES):
            continue
        entries.append(
            {
                "dayOfWeek": DAY_OF_WEEK_NAMES[day],
                "startTime": str(entry.get("start_time", ""))[:5],
                "endTime": str(entry.get("end_time", ""))[:5],
            }
        )
    return entries


def build_resource(resource, schedules) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "@context": HEALTH_RESOURCE_CONTEXT,
        "@type": "hr:HealthResource",
        "healthServiceType": health_service_type(resource, schedules),
        "acceptanceMode": acceptance_mode(schedules),
        "availabilitySchedule": availability_schedule(schedules),
    }
    if scheduling.is_healthcare_service(resource):
        attributes["clinicalDeliveryUnit"] = "TEST"
        attributes["acceptedSchemes"] = [{"@context": CODED_VALUE_CONTEXT, "@type": "Scheme", "code": "PMJAY"}]
    return {
        "id": str(resource.external_id),
        "descriptor": {"name": scheduling.resource_name(resource)},
        "resourceAttributes": attributes,
    }


def build_offer(resource, schedules) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "@context": HEALTH_OFFER_CONTEXT,
        "@type": "hof:HealthOffer",
        "healthServiceType": health_service_type(resource, schedules),
        "offerType": "SINGLE_EVENT",
    }
    if scheduling.is_healthcare_service(resource):
        attributes["outcomeCommitment"] = {
            "attendanceToOutcomeSlaHours": 24,
            "outcomeNoteFormatStandard": "FHIR/DiagnosticReport",
            "mandatory": True,
        }
    return {
        "id": f"offer-{resource.external_id}",
        "resourceIds": [str(resource.external_id)],
        "descriptor": {"name": f"Appointment - {scheduling.resource_name(resource)}"},
        "offerAttributes": attributes,
    }


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "")}


class AppointmentCatalogBuilder(CatalogBuilder):
    def catalogs(self, spec: Any = None) -> list[dict[str, Any]]:
        public_only = getattr(spec, "public_only", True)
        result = []
        for bucket in scheduling.schedules_by_facility(public_only):
            facility = bucket["facility"]
            all_schedules = [s for item in bucket["resources"] for s in item["schedules"]]
            result.append(
                _compact(
                    {
                        "id": f"catalog-{facility.external_id}",
                        "descriptor": {"name": facility.name},
                        "provider": {"id": str(facility.external_id), "descriptor": {"name": facility.name}},
                        # a provider may point consumers at its own BPP; otherwise Care is the BPP of its catalog
                        "bppId": scheduling.beckn_meta(all_schedules, "bppId") or plugin_settings.BECKN_BPP_ID,
                        "bppUri": scheduling.beckn_meta(all_schedules, "bppUri") or plugin_settings.BECKN_BPP_URI,
                        "bapId": plugin_settings.BECKN_BAP_ID,
                        "bapUri": plugin_settings.BECKN_BAP_URI,
                        "resources": [
                            build_resource(item["resource"], item["schedules"]) for item in bucket["resources"]
                        ],
                        "offers": [build_offer(item["resource"], item["schedules"]) for item in bucket["resources"]],
                    }
                )
            )
        return result

    def build(self, spec: Any = None) -> dict[str, Any]:
        """The first catalog; callers that need all of them use ``catalogs``."""
        catalogs = self.catalogs(spec)
        return catalogs[0] if catalogs else {}
