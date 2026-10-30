"""Health-referral vocabulary built on the protocol layer."""

from typing import Any

from care_ccn_be.protocol.schema import Attributes, Contract, JsonLdModel
from care_ccn_be.protocol.schema.health import V21

CODED_VALUE_CONTEXT = "https://raw.githubusercontent.com/beckn/DHP-Specs/main/devkit/stub/context.jsonld"
DISCOVER_SCHEMA_CONTEXT = V21.format("ServiceCoordinationResource")

HEALTH_SERVICE_PHARMACY_DISPENSING = "PHARMACY_DISPENSING"
LIFECYCLE_ACTIVE = "ACTIVE"
LIFECYCLE_FULFILLED = "FULFILLED"


class HealthReferral(JsonLdModel):
    LD_TYPE = "hrf:HealthReferral"
    LD_CONTEXT = V21.format("HealthReferral")

    coordination_id: str | None = None
    coordination_ref: str | None = None
    lifecycle_state: str | None = None
    clinical_urgency_tier: str | None = None
    target_criteria: dict[str, Any] | None = None
    facility_id: str | None = None
    origin_facility_id: str | None = None
    assigned_facility_id: str | None = None
    target_facility_id: str | None = None
    consent: dict[str, Any] | None = None
    referral: dict[str, Any] | None = None


class ReferralContract(Contract):
    contract_attributes: Attributes | None = None


def referral_attributes(contract: Contract | None) -> HealthReferral | None:
    attributes = getattr(contract, "contract_attributes", None)
    return attributes if isinstance(attributes, HealthReferral) else None
