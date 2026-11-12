"""NFH / DHP v2.1 health vocabulary shared by every health flow.

One model per DHP ``*Attributes`` schema (https://github.com/beckn/DHP-Specs/tree/main/devkit/config/schema).
``Service*`` classes are the generic bases the spec extends with ``allOf``; ``Health*`` classes are what goes
on the wire. To carry a new field, declare it on the model that owns it: it is then parsed, validated and
emitted (camelCase) everywhere that model is used. Fields not declared here still pass through untouched.
"""

from datetime import date
from typing import Any

from care_ccn_be.resources.beckn.base import BecknModel, IsoDateTime, JsonLdModel
from care_ccn_be.resources.beckn.codes import (
    ArtifactType,
    AttendanceStatus,
    BookingChannel,
    ClinicalDeliveryUnit,
    Code,
    CoordinationAcceptanceMode,
    DayOfWeek,
    FollowupRecommendation,
    Gender,
    HealthServiceType,
    OfferType,
    ParticipantRole,
    RevocationStatus,
    ServiceUnit,
    UrgencyTier,
)

V21 = "https://schema.beckn.io/{}/v2.1/context.jsonld"
CODED_VALUE_CONTEXT = "https://raw.githubusercontent.com/beckn/DHP-Specs/main/devkit/stub/context.jsonld"


# ----------------------------------------------------------------------------- shared value objects
class CodedValue(JsonLdModel):
    """``{"@context", "@type", "code", "display"}``; subclasses pin the ``@type``."""

    LD_CONTEXT = CODED_VALUE_CONTEXT

    code: str | None = None
    display: str | None = None


class ServiceCategory(CodedValue):
    LD_TYPE = "ServiceCategory"


class Scheme(CodedValue):
    LD_TYPE = "Scheme"


class Specialty(CodedValue):
    LD_TYPE = "Specialty"


class SubSpecialty(CodedValue):
    LD_TYPE = "SubSpecialty"


class ConsentClass(CodedValue):
    LD_TYPE = "ConsentClass"


class ReferralCategory(CodedValue):
    LD_TYPE = "ReferralCategory"


class TargetServiceType(CodedValue):
    LD_TYPE = "TargetServiceType"


class DeviationReasonCode(CodedValue):
    LD_TYPE = "DeviationReasonCode"


class HealthId(BecknModel):
    system: str
    value: str


class Consent(BecknModel):
    consent_class: ConsentClass | None = None
    consent_doc_ref: str | None = None
    consent_id: str | None = None
    patient_participant_id: str | None = None
    consent_standard: str | None = None
    consented_at: IsoDateTime | None = None
    consented_by: str | None = None
    purpose_of_collection: list[str] | None = None
    data_retention_days: int | None = None
    withdrawn_at: IsoDateTime | None = None


class InputArtifact(BecknModel):
    artifact_ref: str
    artifact_type: Code[ArtifactType]
    mime_type: str | None = None
    source_coordination_ref: str | None = None
    access_window_end: IsoDateTime | None = None
    revocation_status: Code[RevocationStatus] | None = None


class ServiceLocation(BecknModel):
    address: str | None = None
    pincode: str | None = None
    location_landmark: str | None = None
    geo: dict[str, Any] | None = None


class AvailabilitySlot(BecknModel):
    day_of_week: Code[DayOfWeek]
    start_time: str
    end_time: str


class CoverageArea(BecknModel):
    iso_code: str | None = None
    region: str | None = None
    country: str | None = None
    coverage_radius_km: float | None = None


class OutcomeCommitment(BecknModel):
    attendance_to_outcome_sla_hours: int | None = None
    outcome_note_format_standard: str | None = None
    mandatory: bool | None = None


class CancellationPolicy(BecknModel):
    hours_before_service: int | None = None
    refund_fraction: float | None = None
    description: str | None = None


# ----------------------------------------------------------------------------- generic Service* bases
class ServiceContract(JsonLdModel):
    booking_channel: Code[BookingChannel] | None = None
    client_notes: str | None = None
    cancellation_reason: str | None = None
    facilitator_id: str | None = None
    reminder_sent_at: IsoDateTime | None = None


class ServiceParticipant(JsonLdModel):
    credential_refs: list[dict[str, Any]] | None = None


class ServicePerformance(JsonLdModel):
    appointment_window_start: IsoDateTime | None = None
    appointment_window_end: IsoDateTime | None = None
    confirmed_appointment_time: IsoDateTime | None = None
    service_location: ServiceLocation | None = None
    provider_confirmed_completion: bool | None = None
    client_confirmed_completion: bool | None = None
    service_notes: str | None = None
    follow_up_required: bool | None = None
    follow_up_date: date | None = None


class ServiceResource(JsonLdModel):
    service_unit: Code[ServiceUnit] | None = None
    coverage_area: list[CoverageArea] | None = None
    capacity_per_day: int | None = None
    capacity_per_week: int | None = None
    availability_schedule: list[AvailabilitySlot] | None = None
    availability_recurrence_rule: str | None = None
    minimum_booking_advance_hours: int | None = None
    languages_supported: list[str] | None = None


class ServiceOffer(JsonLdModel):
    minimum_quantity: int | None = None
    maximum_quantity: int | None = None
    requires_advance_booking: bool | None = None
    bulk_discount_available: bool | None = None
    eligibility_criteria: list[str] | None = None
    validity_period_days: int | None = None
    cancellation_policies: list[CancellationPolicy] | None = None


# ----------------------------------------------------------------------------- Health* attributes
class HealthParticipant(ServiceParticipant):
    LD_TYPE = "hpa:HealthParticipant"
    LD_CONTEXT = V21.format("HealthParticipant")

    participant_role: Code[ParticipantRole] | None = None
    primary_language: str | None = None
    gender: Code[Gender] | None = None
    date_of_birth: date | None = None
    health_ids: list[HealthId] | None = None
    specialist_accreditation: dict[str, Any] | None = None


class HealthPerformance(ServicePerformance):
    LD_TYPE = "hpe:HealthPerformance"
    LD_CONTEXT = V21.format("HealthPerformance")

    health_service_type: Code[HealthServiceType] | None = None
    attendance_status: Code[AttendanceStatus] | None = None
    outcome_note_ref: str | None = None
    outcome_delivered_at: IsoDateTime | None = None
    outcome_sla_breached: bool | None = None
    followup_recommendation_code: Code[FollowupRecommendation] | None = None
    provisional_diagnosis: str | None = None
    artifacts_generated: list[str] | None = None
    deletion_attestation: dict[str, Any] | None = None
    clinical_interpretation: dict[str, Any] | None = None


class HealthContract(ServiceContract):
    LD_TYPE = "hct:HealthContract"
    LD_CONTEXT = V21.format("HealthContract")

    health_service_type: Code[HealthServiceType] | None = None
    coordination_ref: str | None = None
    camp_id: str | None = None
    artifact_access_endpoint: str | None = None
    input_artifacts: list[InputArtifact] | None = None
    consent: Consent | None = None


class HealthResource(ServiceResource):
    LD_TYPE = "hr:HealthResource"
    LD_CONTEXT = V21.format("HealthResource")

    health_service_type: Code[HealthServiceType] | None = None
    clinical_delivery_unit: Code[ClinicalDeliveryUnit] | None = None
    accepted_schemes: list[Scheme] | None = None
    clinical_validation: dict[str, Any] | None = None
    service_prerequisites: list[dict[str, Any]] | None = None
    analytics_engine_specs: dict[str, Any] | None = None
    # Care extension (Schedule.meta["beckn"]["acceptanceMode"]); not part of HealthResource v2.1
    acceptance_mode: str | None = None


class HealthOffer(ServiceOffer):
    LD_TYPE = "hof:HealthOffer"
    LD_CONTEXT = V21.format("HealthOffer")

    health_service_type: Code[HealthServiceType] | None = None
    offer_type: Code[OfferType] | None = None
    outcome_commitment: OutcomeCommitment | None = None


class CoordinationScope(BecknModel):
    referral_categories: list[ReferralCategory] | None = None
    target_service_types: list[TargetServiceType] | None = None
    programme_scope: list[CodedValue] | None = None
    geography_scope: list[CodedValue] | None = None


class ServiceCoordinationResource(ServiceResource):
    LD_TYPE = "scres:ServiceCoordinationResource"
    LD_CONTEXT = V21.format("ServiceCoordinationResource")

    coordination_scope: CoordinationScope | None = None
    acceptance_mode: Code[CoordinationAcceptanceMode] | None = None
    supported_urgency_tiers: list[Code[UrgencyTier]] | None = None


__all__ = [
    "CODED_VALUE_CONTEXT",
    "V21",
    "AvailabilitySlot",
    "CancellationPolicy",
    "CodedValue",
    "Consent",
    "ConsentClass",
    "CoordinationScope",
    "CoverageArea",
    "DeviationReasonCode",
    "HealthContract",
    "HealthId",
    "HealthOffer",
    "HealthParticipant",
    "HealthPerformance",
    "HealthResource",
    "InputArtifact",
    "OutcomeCommitment",
    "ReferralCategory",
    "Scheme",
    "ServiceCategory",
    "ServiceContract",
    "ServiceCoordinationResource",
    "ServiceLocation",
    "ServiceOffer",
    "ServiceParticipant",
    "ServicePerformance",
    "ServiceResource",
    "Specialty",
    "SubSpecialty",
    "TargetServiceType",
]
