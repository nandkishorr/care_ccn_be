"""Contract builders of the referral flow: Care -> network (confirm), the FE-driven BAP requests, BPP callbacks."""

import copy
from dataclasses import dataclass, field
from typing import Any

from care_ccn_be.protocol.actions import (
    CONTRACT_ACTIVE,
    CONTRACT_CANCELLED,
    CONTRACT_COMPLETED,
    CONTRACT_DRAFT,
)
from care_ccn_be.protocol.builder import ContractBuilder, Part
from care_ccn_be.resources.beckn import (
    Commitment,
    Contract,
    Descriptor,
    Document,
    Offer,
    Participant,
    Quantity,
    Resource,
    Status,
)
from care_ccn_be.resources.beckn.codes import ConsultationModality, ParticipantRole
from care_ccn_be.resources.beckn.health import HealthParticipant, ServiceCategory
from care_ccn_be.resources.referral.schema import (
    HEALTH_SERVICE_PHARMACY_DISPENSING,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_FULFILLED,
    HealthReferral,
    ReferralAttachment,
    ReferralBlock,
    ReferralContract,
    ReferralFormResponse,
    ReferralNote,
    ReferralPatient,
    TargetCriteria,
)
from care_ccn_be.services import availability as availabilities
from care_ccn_be.services import encounter as encounters
from care_ccn_be.services import mappers
from care_ccn_be.services.identifiers import health_ids_from_patient

PATIENT_ROLE = ParticipantRole.PATIENT
SOURCE_CATEGORY = "source"
CATEGORY_FOR_TYPE = {mappers.REFERRAL_TYPE_CONSULTATION: "patient_care", mappers.REFERRAL_TYPE_LAB: "other"}
TYPE_FOR_CATEGORY = {"patient_care": mappers.REFERRAL_TYPE_CONSULTATION}


def referral_type_for_category(category: str | None) -> str:
    """The Care referral type for a FE category: patient care is a consultation, anything else a lab referral."""
    return TYPE_FOR_CATEGORY.get(category or "", mappers.REFERRAL_TYPE_LAB)


def service_category_criteria(category: str | None) -> TargetCriteria:
    """``targetCriteria`` for a category: a consultation for patient care, else an investigation."""
    if category == "patient_care":
        return TargetCriteria(
            service_category=ServiceCategory(code="CONSULTATION", display="Consultation"),
            procedure_needs=["HOME_VISIT"],
            consultation_modality=ConsultationModality.IN_PERSON,
        )
    return TargetCriteria(
        service_category=ServiceCategory(code="INVESTIGATION", display="Investigation"),
        consultation_modality=ConsultationModality.IN_PERSON,
        health_service_type=HEALTH_SERVICE_PHARMACY_DISPENSING if category == "medicines" else None,
    )


def patient_participant(participant_id: str, name: str, health_ids=(), gender=None, date_of_birth=None) -> Participant:
    return Participant(
        id=participant_id,
        descriptor=Descriptor(name=name),
        participant_attributes=HealthParticipant(
            participant_role=PATIENT_ROLE,
            health_ids=list(health_ids),
            gender=gender,
            date_of_birth=date_of_birth,
        ),
    )


def care_patient_participant(patient, participant_id: str | None = None) -> Participant:
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
        return Status(code=builder.status_code)


class CareDescriptorPart(Part):
    field = "descriptor"

    def build(self, spec, builder):
        return Descriptor(name=spec.referral.title, short_desc=spec.referral.note or None)


class CareCommitmentsPart(Part):
    field = "commitments"

    def build(self, spec, builder):
        rid = str(spec.referral.external_id)
        return [
            Commitment(
                id=f"commitment-{rid}",
                status=Status(descriptor=Descriptor(code=builder.status_code)),
                resources=[Resource(id=rid, quantity=Quantity(count=1))],
                offer=Offer(id=f"offer-{rid}", resource_ids=[rid]),
            )
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
        return HealthReferral(
            coordination_id=spec.coordination_id,
            lifecycle_state=LIFECYCLE_ACTIVE,
            clinical_urgency_tier=spec.urgency,
            target_criteria=service_category_criteria(spec.category),
            facility_id=origin or assigned,
            origin_facility_id=origin,
            assigned_facility_id=assigned,
        )


class ReferralCareConfirmBuilder(ContractBuilder):
    status_code = CONTRACT_ACTIVE
    parts = (StatusPart, CareDescriptorPart, CareCommitmentsPart, CareParticipantsPart, CareAttributesPart)


# ----------------------------------------------------------------------------- FE-driven BAP requests
@dataclass
class ReferralRequest:
    """What the BAP request builders read: the request spec's values, the Care patient and the transaction."""

    txn: Any
    state: Any
    offer_id: str | None = None
    facility: str | None = None
    patient: Any = None
    patient_name: str | None = None
    title: str | None = None
    reason: str | None = None
    category: str | None = None
    assigned_facility_id: str | None = None
    emergency: bool = False
    priority: int | None = None

    @classmethod
    def of(cls, request_obj, txn, state) -> "ReferralRequest":
        from care.emr.models import Patient
        from care.utils.shortcuts import get_object_or_404

        values = request_obj.params()
        patient = values.pop("patient", None)
        encounter = values.pop("encounter", None)
        if patient:
            patient = get_object_or_404(Patient, external_id=patient)
        elif encounter:
            patient = encounters.get_by_external_id(encounter).patient
        return cls(txn=txn, state=state, patient=patient or None, **values)


class OfferCommitmentPart(Part):
    field = "commitments"

    def build(self, spec, builder):
        offer_id = spec.offer_id or spec.state.offer_id
        if not offer_id:
            return None
        return [Commitment(id=f"commitment-{spec.txn.transaction_id}", offer=Offer(id=offer_id))]


class BapDescriptorPart(Part):
    field = "descriptor"

    def build(self, spec, builder):
        return Descriptor(name=spec.title or "Care referral", short_desc=spec.reason or None)


class BapParticipantsPart(Part):
    field = "participants"

    def build(self, spec, builder):
        if spec.patient is not None:
            return [care_patient_participant(spec.patient)]
        if spec.patient_name:
            return [patient_participant(f"participant-patient-{spec.txn.transaction_id}", spec.patient_name)]
        return None


class BapAttributesPart(Part):
    field = "contract_attributes"

    def build(self, spec, builder):
        request = {}
        if builder.carries_request:
            request = {
                "facility_id": spec.facility,
                "origin_facility_id": spec.facility,
                "assigned_facility_id": spec.assigned_facility_id,
                "clinical_urgency_tier": mappers.urgency_tier(spec.emergency, spec.priority),
                "target_criteria": service_category_criteria(spec.category or "other"),
            }
        return HealthReferral(
            coordination_id=spec.txn.coordination_id or spec.txn.transaction_id,
            lifecycle_state=builder.lifecycle_state,
            **request,
        )


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


def referral_block(referral, details: dict[str, Any] | None = None) -> ReferralBlock:
    """``details`` is the ``ReferralLink.details`` Care keeps for the referral (urgency, origin facility)."""
    details = details or {}
    urgency = details.get("urgency") or mappers.URGENCY_ROUTINE
    patient = referral.patient
    return ReferralBlock(
        id=str(referral.external_id),
        status=referral.status,
        title=referral.title,
        reason=referral.note,
        emergency=urgency == mappers.URGENCY_EMERGENCY,
        priority=mappers.URGENCY_PRIORITY.get(urgency),
        category=CATEGORY_FOR_TYPE.get(referral.availability.referral_type, "other"),
        referral_type=referral.availability.referral_type,
        origin_facility_id=details.get("originFacilityId") or None,
        patient=ReferralPatient(
            id=str(patient.external_id),
            name=patient.name,
            gender=mappers.map_gender_to_nfh(patient.gender),
            health_ids=health_ids_from_patient(patient) or None,
        ),
    )


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
        contract = ReferralContract.model_validate(body["contract"])
        attributes = contract.contract_attributes or HealthReferral()
        attributes.lifecycle_state = lifecycle
        attributes.referral = referral_block(referral, details)
        contract.contract_attributes = attributes
        contract.id = contract.id or str(referral.external_id)
        body["contract"] = contract.dump()
        strip_pricing(body["contract"])
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

    @classmethod
    def on_update(cls, message: dict[str, Any], referral, details=None) -> dict[str, Any]:
        """The shared documents are not echoed back: the sender already has them."""
        body = cls.overlay(message, referral, details)
        (body["contract"].get("descriptor") or {}).pop("docs", None)
        if body["contract"].get("descriptor") == {}:
            body["contract"].pop("descriptor")
        return body


def sync_message(
    coordination_id: str,
    documents: list[Document],
    attachments: list[ReferralAttachment],
    responses: list[ReferralFormResponse],
    status: str | None = None,
    contract_id: str | None = None,
    external_referral_id: str | None = None,
) -> dict[str, Any]:
    """What one side of a referral added since its last sync: files (``descriptor.docs[]``, source ones also as
    the referral note's prior results), answered forms and, from the receiving side, the referral status."""
    source_refs = [attachment.ref for attachment in attachments if attachment.category == SOURCE_CATEGORY]
    attributes = HealthReferral(
        coordination_id=coordination_id,
        referral_note=ReferralNote(prior_result_refs=source_refs) if source_refs else None,
        referral=ReferralBlock(
            id=external_referral_id, status=status, attachments=attachments or None, responses=responses or None
        ),
    )
    contract = Contract(
        id=contract_id,
        descriptor=Descriptor(docs=documents) if documents else None,
        contract_attributes=attributes,
    )
    if status:
        lifecycle = mappers.map_status_to_lifecycle(status)
        attributes.lifecycle_state = lifecycle
        contract.status = Status(code=LIFECYCLE_TO_CONTRACT_STATUS.get(lifecycle, CONTRACT_ACTIVE))
    return {"contract": contract.dump()}


def completion_message(coordination_id: str) -> dict[str, Any]:
    """The unsolicited ``on_update`` that tells the origin its referral was fulfilled."""
    contract = Contract(
        status=Status(code=CONTRACT_COMPLETED),
        contract_attributes=HealthReferral(coordination_id=coordination_id, lifecycle_state=LIFECYCLE_FULFILLED),
    )
    return {"contract": contract.dump()}


def clone(message: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(message)
