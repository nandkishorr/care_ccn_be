"""Mapping between NFH (DHP) values and Care values, plus tolerant readers for raw inbound message dicts."""

from datetime import date
from typing import Any

from care.emr.resources.patient.spec import GenderChoices
from care.emr.resources.resource_request.spec import StatusChoices

LIFECYCLE_DRAFT = "DRAFT"
LIFECYCLE_ACTIVE = "ACTIVE"
LIFECYCLE_BOOKING_CONFIRMED = "BOOKING_CONFIRMED"
LIFECYCLE_CANCELLED = "CANCELLED"
LIFECYCLE_FULFILLED = "FULFILLED"

PARTICIPANT_ROLE_PATIENT = "PATIENT"
CONTACT_ROLES = ("REFERRER", "PROVIDER", "COORDINATOR", "PRACTITIONER")

NFH_OTHER_GENDER = "OTHER"

GENDER_TO_CARE = {
    "MALE": GenderChoices.male.value,
    "FEMALE": GenderChoices.female.value,
    "OTHER": GenderChoices.non_binary.value,
    "PREFER_NOT_TO_SAY": GenderChoices.non_binary.value,
    "TRANSGENDER": GenderChoices.non_binary.value,
}

# The NFH schema only accepts MALE/FEMALE/OTHER/PREFER_NOT_TO_SAY.
GENDER_TO_NFH = {
    GenderChoices.male.value: "MALE",
    GenderChoices.female.value: "FEMALE",
    GenderChoices.non_binary.value: NFH_OTHER_GENDER,
    GenderChoices.transgender.value: NFH_OTHER_GENDER,
}

STATUS_TO_LIFECYCLE = {
    StatusChoices.pending.value: LIFECYCLE_DRAFT,
    StatusChoices.approved.value: LIFECYCLE_ACTIVE,
    StatusChoices.rejected.value: LIFECYCLE_CANCELLED,
    StatusChoices.cancelled.value: LIFECYCLE_CANCELLED,
    StatusChoices.transportation_to_be_arranged.value: LIFECYCLE_ACTIVE,
    StatusChoices.transfer_in_progress.value: LIFECYCLE_BOOKING_CONFIRMED,
    StatusChoices.completed.value: LIFECYCLE_FULFILLED,
}

URGENCY_EMERGENCY = "EMERGENCY"
URGENCY_URGENT = "URGENT"
URGENCY_ROUTINE = "ROUTINE"
URGENCY_PRIORITY = {URGENCY_EMERGENCY: 3, URGENCY_URGENT: 2, URGENCY_ROUTINE: 1}
URGENT_PRIORITY_THRESHOLD = 2


def map_gender(nfh_gender: str | None) -> str:
    """NFH gender -> Care gender; missing or unknown values are ``non_binary``, never a guess."""
    return GENDER_TO_CARE.get((nfh_gender or "").strip().upper(), GenderChoices.non_binary.value)


def map_gender_to_nfh(care_gender: str | None) -> str:
    """Care gender -> NFH gender; missing or unknown values are ``OTHER``."""
    return GENDER_TO_NFH.get(care_gender or "", NFH_OTHER_GENDER)


def map_status_to_lifecycle(status: str | None) -> str:
    return STATUS_TO_LIFECYCLE.get(status or "", LIFECYCLE_DRAFT)


def urgency_tier(emergency: bool, priority: int | None) -> str:
    if emergency:
        return URGENCY_EMERGENCY
    if priority and priority >= URGENT_PRIORITY_THRESHOLD:
        return URGENCY_URGENT
    return URGENCY_ROUTINE


def get_contract(message: dict[str, Any] | None) -> dict[str, Any]:
    return (message or {}).get("contract") or {}


def get_contract_attributes(message: dict[str, Any] | None) -> dict[str, Any]:
    return get_contract(message).get("contractAttributes") or {}


def find_patient_participant(message: dict[str, Any] | None) -> dict[str, Any] | None:
    for participant in get_contract(message).get("participants") or []:
        attributes = participant.get("participantAttributes") or {}
        if attributes.get("participantRole") == PARTICIPANT_ROLE_PATIENT:
            return participant
    return None


def extract_health_ids(participant: dict[str, Any] | None) -> list[dict[str, Any]]:
    attributes = (participant or {}).get("participantAttributes") or {}
    return [
        {"system": item.get("system"), "value": item.get("value")}
        for item in attributes.get("healthIds") or []
        if item.get("value")
    ]


def extract_date_of_birth(participant: dict[str, Any] | None) -> date | None:
    value = ((participant or {}).get("participantAttributes") or {}).get("dateOfBirth")
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def referral_contact(message: dict[str, Any] | None) -> dict[str, str]:
    for participant in get_contract(message).get("participants") or []:
        attributes = participant.get("participantAttributes") or {}
        if attributes.get("participantRole") in CONTACT_ROLES:
            telecom = attributes.get("telecom") or attributes.get("phone") or ""
            return {
                "referring_facility_contact_name": ((participant.get("descriptor") or {}).get("name") or "")[:255],
                "referring_facility_contact_number": str(telecom)[:14],
            }
    return {"referring_facility_contact_name": "", "referring_facility_contact_number": ""}


def referral_fields(message: dict[str, Any] | None) -> dict[str, Any]:
    """ResourceRequest field values derived from an inbound HealthReferral contract."""
    attributes = get_contract_attributes(message)
    criteria = attributes.get("targetCriteria") or {}
    urgency = attributes.get("clinicalUrgencyTier") or criteria.get("urgencyTier")
    specialty_value = criteria.get("specialty") or {}
    specialty = specialty_value.get("display") or specialty_value.get("code")
    title = (get_contract(message).get("descriptor") or {}).get("name") or (
        f"NFH Referral - {specialty}" if specialty else "NFH Referral"
    )
    return {
        "title": title[:255],
        "reason": (attributes.get("consent") or {}).get("clinicalJustification")
        or (get_contract(message).get("descriptor") or {}).get("shortDesc")
        or specialty
        or "",
        "emergency": urgency == URGENCY_EMERGENCY,
        "priority": URGENCY_PRIORITY.get(urgency),
        "category": "patient_care",
        **referral_contact(message),
    }
