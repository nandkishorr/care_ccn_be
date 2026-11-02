"""Contract builders of the referral flow: Care -> network (confirm), the FE-driven BAP requests, BPP callbacks."""

import copy
from dataclasses import dataclass, field
from typing import Any

from care_ccn_be.flows.referral.schema import (
    CODED_VALUE_CONTEXT,
    HEALTH_SERVICE_PHARMACY_DISPENSING,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_FULFILLED,
    HealthReferral,
)
from care_ccn_be.protocol.actions import (
    CONTRACT_ACTIVE,
    CONTRACT_CANCELLED,
    CONTRACT_COMPLETED,
    CONTRACT_DRAFT,
)
from care_ccn_be.protocol.builder import ContractBuilder, Part
from care_ccn_be.protocol.schema import Contract
from care_ccn_be.protocol.schema.health import HealthParticipant
from care_ccn_be.services import availability as availabilities
from care_ccn_be.services import mappers
from care_ccn_be.services.identifiers import health_ids_from_patient

PATIENT_ROLE = mappers.PARTICIPANT_ROLE_PATIENT
CATEGORY_FOR_TYPE = {mappers.REFERRAL_TYPE_CONSULTATION: "patient_care", mappers.REFERRAL_TYPE_LAB: "other"}
TYPE_FOR_CATEGORY = {"patient_care": mappers.REFERRAL_TYPE_CONSULTATION}


def referral_type_for_category(category: str | None) -> str:
    """The Care referral type for a FE category: patient care is a consultation, anything else a lab referral."""
    return TYPE_FOR_CATEGORY.get(category or "", mappers.REFERRAL_TYPE_LAB)


def service_category_criteria(category: str | None) -> dict[str, Any]:
    """``targetCriteria`` for a category: a consultation for patient care, else an investigation."""
    if category == "patient_care":
        return {
            "serviceCategory": {
                "@context": CODED_VALUE_CONTEXT,
                "@type": "ServiceCategory",
                "code": "CONSULTATION",
                "display": "Consultation",
            },
            "procedureNeeds": ["HOME_VISIT"],
            "consultationModality": "IN_PERSON",
        }
    criteria: dict[str, Any] = {
        "serviceCategory": {
            "@context": CODED_VALUE_CONTEXT,
            "@type": "ServiceCategory",
            "code": "INVESTIGATION",
            "display": "Investigation",
        },
        "consultationModality": "IN_PERSON",
    }
    if category == "medicines":
        criteria["healthServiceType"] = HEALTH_SERVICE_PHARMACY_DISPENSING
    return criteria


def patient_participant(participant_id: str, name: str, health_ids=(), gender=None, date_of_birth=None) -> dict:
    return {
        "id": participant_id,
        "descriptor": {"name": name},
        "participantAttributes": HealthParticipant(
            participant_role=PATIENT_ROLE,
            health_ids=list(health_ids),
            gender=gender,
            date_of_birth=date_of_birth,
        ),
    }


def care_patient_participant(patient, participant_id: str | None = None) -> dict:
    return patient_participant(
        participant_id or f"participant-patient-{patient.external_id}",
        patient.name,
        health_ids_from_patient(patient),
        gender=mappers.map_gender_to_nfh(patient.gender) if patient.gender else None,
        date_of_birth=patient.date_of_birth,
    )


# ----------------------------------------------------------------------------- Care -> network confirm
@dataclass
class CareReferral:
    referral: Any
    coordination_id: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def target(self) -> dict[str, Any]:
        return availabilities.target_of(self.referral.availability)

    @property
    def urgency(self) -> str:
        return self.details.get("urgency") or mappers.URGENCY_ROUTINE

    @property
    def category(self) -> str:
        return CATEGORY_FOR_TYPE.get(self.referral.availability.referral_type, "other")

    @property
    def origin_facility_id(self) -> str | None:
        """The facility the referral is raised from: the encounter's, else what the link remembers."""
        referral = self.referral
        if referral.encounter_id:
            return str(referral.encounter.facility.external_id)
        return self.details.get("originFacilityId")

    @property
    def assigned_facility_id(self) -> str | None:
        return self.target.get("assignedFacilityId")


class StatusPart(Part):
    field = "status"

    def build(self, spec, builder):
        return {"code": builder.status_code}


class CareDescriptorPart(Part):
    field = "descriptor"

    def build(self, spec, builder):
        return {"name": spec.referral.title, "shortDesc": spec.referral.note or None}


class CareCommitmentsPart(Part):
    field = "commitments"

    def build(self, spec, builder):
        rid = str(spec.referral.external_id)
        return [
            {
                "id": f"commitment-{rid}",
                "status": {"descriptor": {"code": builder.status_code}},
                "resources": [{"id": rid, "quantity": {"count": 1}}],
                "offer": {"id": f"offer-{rid}", "resourceIds": [rid]},
            }
        ]


class CareParticipantsPart(Part):
    field = "participants"

    def build(self, spec, builder):
        return [care_patient_participant(spec.referral.patient)]


class CareAttributesPart(Part):
    """The origin facility is ``facilityId`` so the receiver resolves the origin referral; the target facility
    lives on the network availability, since it may exist only in the other instance."""

    field = "contract_attributes"

    def build(self, spec, builder):
        origin, assigned = spec.origin_facility_id, spec.assigned_facility_id
        values = {
            "coordination_id": spec.coordination_id,
            "lifecycle_state": LIFECYCLE_ACTIVE,
            "clinical_urgency_tier": spec.urgency,
            "target_criteria": service_category_criteria(spec.category),
            "facility_id": origin or assigned,
            "origin_facility_id": origin,
            "assigned_facility_id": assigned,
        }
        return HealthReferral(**{key: value for key, value in values.items() if value is not None})


class ReferralCareConfirmBuilder(ContractBuilder):
    status_code = CONTRACT_ACTIVE
    parts = (StatusPart, CareDescriptorPart, CareCommitmentsPart, CareParticipantsPart, CareAttributesPart)


# ----------------------------------------------------------------------------- FE-driven BAP requests
class OfferCommitmentPart(Part):
    field = "commitments"

    def build(self, spec, builder):
        offer_id = spec.offer_id or spec.state.offer_id
        if not offer_id:
            return None
        return [{"id": f"commitment-{spec.txn.transaction_id}", "offer": {"id": offer_id}}]


class BapDescriptorPart(Part):
    field = "descriptor"

    def build(self, spec, builder):
        return {"name": spec.title or "Care referral", "shortDesc": spec.reason or None}


class BapParticipantsPart(Part):
    field = "participants"

    def build(self, spec, builder):
        if spec.spec.patient:
            return [care_patient_participant(spec.patient)]
        if spec.patient_name:
            return [patient_participant(f"participant-patient-{spec.txn.transaction_id}", spec.patient_name)]
        return None


class BapAttributesPart(Part):
    field = "contract_attributes"

    def build(self, spec, builder):
        values = {
            "coordination_id": spec.txn.coordination_id or spec.txn.transaction_id,
            "lifecycle_state": builder.lifecycle_state,
        }
        if builder.carries_request:
            category = spec.category or "other"
            values.update(
                facility_id=spec.facility,
                origin_facility_id=spec.facility,
                assigned_facility_id=spec.assigned_facility_id,
                clinical_urgency_tier=mappers.urgency_tier(spec.emergency, spec.priority),
                target_criteria=service_category_criteria(category),
            )
        return HealthReferral(**{key: value for key, value in values.items() if value is not None})


class ReferralBapBuilder(ContractBuilder):
    lifecycle_state: str = LIFECYCLE_ACTIVE
    carries_request: bool = True


class ReferralSelectBuilder(ReferralBapBuilder):
    status_code = CONTRACT_DRAFT
    carries_request = False
    parts = (StatusPart, OfferCommitmentPart, BapAttributesPart)


class ReferralInitBuilder(ReferralBapBuilder):
    status_code = CONTRACT_DRAFT
    parts = (StatusPart, BapDescriptorPart, OfferCommitmentPart, BapParticipantsPart, BapAttributesPart)


class ReferralConfirmBuilder(ReferralBapBuilder):
    status_code = CONTRACT_ACTIVE
    parts = (StatusPart, BapDescriptorPart, OfferCommitmentPart, BapParticipantsPart, BapAttributesPart)


# ----------------------------------------------------------------------------- BPP callbacks
LIFECYCLE_TO_CONTRACT_STATUS = {
    mappers.LIFECYCLE_DRAFT: CONTRACT_DRAFT,
    mappers.LIFECYCLE_CANCELLED: CONTRACT_CANCELLED,
    mappers.LIFECYCLE_FULFILLED: CONTRACT_COMPLETED,
}


def strip_pricing(contract: dict[str, Any]) -> None:
    """Care referrals carry no cost."""
    contract.pop("consideration", None)
    contract.pop("quote", None)
    for commitment in contract.get("commitments") or []:
        commitment.pop("considerations", None)
        if isinstance(commitment.get("offer"), dict):
            commitment["offer"].pop("considerations", None)


def referral_block(referral, details: dict[str, Any] | None = None) -> dict[str, Any]:
    details = details or {}
    urgency = details.get("urgency") or mappers.URGENCY_ROUTINE
    block: dict[str, Any] = {
        "id": str(referral.external_id),
        "status": referral.status,
        "title": referral.title,
        "reason": referral.note,
        "emergency": urgency == mappers.URGENCY_EMERGENCY,
        "priority": mappers.URGENCY_PRIORITY.get(urgency),
        "category": CATEGORY_FOR_TYPE.get(referral.availability.referral_type, "other"),
        "referralType": referral.availability.referral_type,
    }
    if details.get("originFacilityId"):
        block["originFacilityId"] = details["originFacilityId"]
    patient = referral.patient
    block["patient"] = {
        "id": str(patient.external_id),
        "name": patient.name,
        "gender": mappers.map_gender_to_nfh(patient.gender),
    }
    health_ids = health_ids_from_patient(patient)
    if health_ids:
        block["patient"]["healthIds"] = health_ids
    return block


class ReferralEcho(ContractBuilder):
    """BPP callbacks echo the inbound contract and overlay the state of the Care referral."""

    @classmethod
    def on_select(cls, message: dict[str, Any]) -> dict[str, Any]:
        body = cls.echo(message, CONTRACT_DRAFT)
        strip_pricing(body["contract"])
        return body

    @classmethod
    def overlay(
        cls, message: dict[str, Any], referral, details: dict[str, Any] | None = None, status_code: str | None = None
    ) -> dict[str, Any]:
        lifecycle = mappers.map_status_to_lifecycle(referral.status)
        body = cls.echo(message, status_code or LIFECYCLE_TO_CONTRACT_STATUS.get(lifecycle, CONTRACT_ACTIVE))
        contract = body["contract"]
        contract.setdefault("id", str(referral.external_id))
        attributes = contract.setdefault("contractAttributes", {})
        attributes.setdefault("@context", HealthReferral.LD_CONTEXT)
        attributes.setdefault("@type", HealthReferral.LD_TYPE)
        attributes["lifecycleState"] = lifecycle
        attributes["referral"] = referral_block(referral, details)
        strip_pricing(contract)
        return body

    @classmethod
    def on_init(cls, message: dict[str, Any], referral, details=None) -> dict[str, Any]:
        return cls.overlay(message, referral, details, CONTRACT_DRAFT)

    @classmethod
    def on_confirm(cls, message: dict[str, Any], referral, details=None) -> dict[str, Any]:
        return cls.overlay(message, referral, details, CONTRACT_ACTIVE)

    @classmethod
    def on_status(cls, message: dict[str, Any], referral, details=None) -> dict[str, Any]:
        return cls.overlay(message, referral, details)


def completion_message(coordination_id: str) -> dict[str, Any]:
    """The unsolicited ``on_update`` that tells the origin its referral was fulfilled."""
    contract = Contract(
        status={"code": CONTRACT_COMPLETED},
        contract_attributes=HealthReferral(coordination_id=coordination_id, lifecycle_state=LIFECYCLE_FULFILLED),
    )
    return {"contract": contract.dump()}


def clone(message: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(message)
