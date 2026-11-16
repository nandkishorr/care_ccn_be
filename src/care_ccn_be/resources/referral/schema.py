"""Health-referral vocabulary built on the protocol layer (DHP ``HealthReferral`` v2.1 + Care extensions)."""

from typing import Annotated, Any

from pydantic import BeforeValidator, model_validator

from care_ccn_be.resources.beckn import BecknModel, Contract, ContractMessage, Offer
from care_ccn_be.resources.beckn.base import IsoDateTime, JsonLdModel
from care_ccn_be.resources.beckn.codes import (
    Code,
    ConsultationModality,
    Gender,
    HealthServiceType,
    ReferralLifecycleState,
    RevocationStatus,
    UrgencyTier,
)
from care_ccn_be.resources.beckn.health import (
    V21,
    CodedValue,
    Consent,
    DeviationReasonCode,
    HealthId,
    ServiceCategory,
    Specialty,
    SubSpecialty,
)

DISCOVER_SCHEMA_CONTEXT = V21.format("HealthResource")

HEALTH_SERVICE_PHARMACY_DISPENSING = HealthServiceType.PHARMACY_DISPENSING
LIFECYCLE_ACTIVE = ReferralLifecycleState.ACTIVE
LIFECYCLE_FULFILLED = "FULFILLED"  # Care's completion state; not in the DHP lifecycle (adapter may reject it)
OFFER_REQUIRED = "offer_id is required to select a coordinator"
FACILITY_REQUIRED = "facility is required (or contractAttributes.facilityId in a passthrough message)"


class TargetCriteria(BecknModel):
    """``ServiceCoordination.targetCriteria`` extended by ``HealthReferral``."""

    service_category: ServiceCategory | None = None
    geography_constraint: list[CodedValue] | None = None
    scheme_required: list[CodedValue] | None = None
    modality: str | None = None
    urgency_tier: Code[UrgencyTier] | None = None
    time_window: dict[str, Any] | None = None
    specialty: Specialty | None = None
    sub_specialty: SubSpecialty | None = None
    equipment_needs: list[str] | None = None
    procedure_needs: list[str] | None = None
    consultation_modality: Code[ConsultationModality] | None = None
    # Care extension: narrows an investigation to a health service type
    health_service_type: Code[HealthServiceType] | None = None


class ReferralConsent(Consent):
    mediator_credential_ref: str | None = None
    originating_consent_ref: str | None = None
    deemed_basis_code: str | None = None
    clinical_justification: str | None = None
    ratified_at: IsoDateTime | None = None


class ReferralNote(BecknModel):
    artifact_ref: str | None = None
    credential_scope: str | None = None
    access_window_start: IsoDateTime | None = None
    access_window_end: IsoDateTime | None = None
    revocation_status: Code[RevocationStatus] | None = None
    revoked_at: IsoDateTime | None = None
    specialty: str | None = None
    urgency: Code[UrgencyTier] | None = None
    prior_result_refs: list[str] | None = None


class RecommendedTarget(BecknModel):
    target_provider_id: str | None = None
    target_resource_ref: str | None = None
    target_offer_ref: str | None = None
    terms_snapshot: dict[str, Any] | None = None
    snapshot_at: IsoDateTime | None = None


class HandoverDocument(BecknModel):
    artifact_ref: str | None = None
    credential_scope: str | None = None
    access_window_start: IsoDateTime | None = None
    access_window_end: IsoDateTime | None = None
    revocation_status: Code[RevocationStatus] | None = None
    revoked_at: IsoDateTime | None = None


class NotificationRecipient(BecknModel):
    party_ref: str | None = None
    party_role: str | None = None
    notification_scope: list[str] | str | None = None
    channel_ref: str | None = None


class Deviation(BecknModel):
    reason_code: DeviationReasonCode | None = None
    detected_by: str | None = None
    detected_at: IsoDateTime | None = None
    note: str | None = None


class Handoff(BecknModel):
    new_coordinator_id: str | None = None
    handoff_at: IsoDateTime | None = None


class ReferralPatient(BecknModel):
    id: str | None = None
    name: str | None = None
    gender: Code[Gender] | None = None
    health_ids: list[HealthId] | None = None


class ReferralAttachment(BecknModel):
    """Care extension: a shared file; ``ref`` is the label of its ``Descriptor.docs[]`` entry."""

    ref: str
    category: str | None = None  # Care's ReferralResource attachment_type: source / metadata / response
    name: str | None = None
    extension: str | None = None


class ReferralFormResponse(BecknModel):
    """Care extension: a questionnaire answered on the referral, with the questionnaire as it was answered."""

    ref: str
    questionnaire_type: str | None = None
    status: str | None = None
    revision: int | None = None
    questionnaire: dict[str, Any] | None = None
    questionnaire_response: dict[str, Any] | None = None
    created_at: IsoDateTime | None = None
    author: str | None = None


class ReferralBlock(BecknModel):
    """Care extension: the state of the Care referral, overlaid by the BPP on its callbacks."""

    id: str | None = None
    status: str | None = None
    title: str | None = None
    reason: str | None = None
    emergency: bool | None = None
    priority: int | None = None
    category: str | None = None
    referral_type: str | None = None
    origin_facility_id: str | None = None
    patient: ReferralPatient | None = None
    attachments: list[ReferralAttachment] | None = None
    responses: list[ReferralFormResponse] | None = None


class HealthReferral(JsonLdModel):
    LD_TYPE = "hrf:HealthReferral"
    LD_CONTEXT = V21.format("HealthReferral")

    # ServiceCoordination v2.1
    coordination_id: str | None = None
    lifecycle_state: Code[ReferralLifecycleState] | None = None
    target_criteria: TargetCriteria | None = None
    recommended_target_ref: RecommendedTarget | None = None
    target_booking_ref: str | None = None
    handover_document: HandoverDocument | None = None
    notification_roster: list[NotificationRecipient] | None = None
    deviation: Deviation | None = None
    grievance_ref: str | None = None
    handoff: Handoff | None = None
    # HealthReferral v2.1
    consent: ReferralConsent | None = None
    referral_note: ReferralNote | None = None
    scheme_ref: CodedValue | None = None
    clinical_urgency_tier: Code[UrgencyTier] | None = None
    # Care extensions
    coordination_ref: str | None = None
    facility_id: str | None = None
    origin_facility_id: str | None = None
    assigned_facility_id: str | None = None
    target_facility_id: str | None = None
    referral: ReferralBlock | None = None


def _as_referral(value: Any) -> Any:
    """Referral contracts always carry ``HealthReferral`` attributes, even when ``@type`` is omitted."""
    if isinstance(value, JsonLdModel) and not isinstance(value, HealthReferral):
        return value.model_dump(by_alias=True, exclude_unset=True)
    return value


ReferralAttributes = Annotated[HealthReferral, BeforeValidator(_as_referral)]


class ReferralContract(Contract):
    contract_attributes: ReferralAttributes | None = None
    offers: list[Offer] = []

    @property
    def offer_id(self) -> str | None:
        offers = [*self.offers, *(c.offer for c in self.commitments if c.offer)]
        return next((offer.id for offer in offers if offer.id), None)

    @property
    def facility_id(self) -> str | None:
        attributes = self.contract_attributes
        if attributes is None:
            return None
        return attributes.facility_id or attributes.origin_facility_id


class ReferralMessage(ContractMessage[ReferralContract]):
    pass


class ReferralSelectMessage(ReferralMessage):
    @model_validator(mode="after")
    def _offer_required(self):
        if not self.contract.offer_id:
            raise ValueError(OFFER_REQUIRED)
        return self


class ReferralConfirmMessage(ReferralMessage):
    @model_validator(mode="after")
    def _facility_required(self):
        if not self.contract.facility_id:
            raise ValueError(FACILITY_REQUIRED)
        return self


def referral_attributes(contract: Contract | None) -> HealthReferral | None:
    attributes = getattr(contract, "contract_attributes", None)
    return attributes if isinstance(attributes, HealthReferral) else None
