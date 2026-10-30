"""Patient lookups shared by flows. Flows go through here instead of importing Care models directly."""

from rest_framework.exceptions import NotFound


def get_by_external_id(external_id):
    from care.emr.models import Patient

    patient = Patient.objects.filter(external_id=external_id).first()
    if patient is None:
        raise NotFound("Unknown patient")
    return patient


PLACEHOLDER_PHONE_NUMBER = "0000000000"  # Care's Patient requires a phone; emergency referrals may not carry one
DEFAULT_REFERRAL_AGE_YEARS = 35  # referrals without a date of birth are recorded as this age
UNIDENTIFIED_PATIENT = "Unidentified Patient"
PHONE_SYSTEMS = {"phone", "mobile", "sms", "tel", ""}


def _phone_from_contacts(contacts) -> str | None:
    for contact in contacts or []:
        if not isinstance(contact, dict):
            continue
        system = (contact.get("system") or contact.get("type") or "").lower()
        value = contact.get("value") or contact.get("phone") or contact.get("number")
        if value and system in PHONE_SYSTEMS:
            return value
    return None


def resolve_phone(contract_attributes: dict, participant: dict) -> str:
    attributes = participant.get("participantAttributes") or {}
    descriptor = participant.get("descriptor") or {}
    explicit = (
        attributes.get("phone")
        or attributes.get("phoneNumber")
        or descriptor.get("phone")
        or descriptor.get("phoneNumber")
        or _phone_from_contacts(attributes.get("telecom"))
        or _phone_from_contacts(attributes.get("contacts"))
        or _phone_from_contacts(participant.get("contacts"))
    )
    if explicit and str(explicit).strip():
        return str(explicit).strip()[-14:]
    for entry in contract_attributes.get("notificationRoster") or []:
        channel = entry.get("channelRef") or ""
        if channel.startswith("sms:") and (
            entry.get("partyRole") == "SUBJECT" or entry.get("partyRef") == participant.get("id")
        ):
            number = channel.split("sms:", 1)[1].strip()
            if number:
                return number[-14:]
    return PLACEHOLDER_PHONE_NUMBER


def _match_existing(health_ids: list[dict], phone: str, name: str, gender: str, date_of_birth):
    from care.emr.models import Patient

    from care_ccn_be.services.identifiers import find_patient_by_health_ids

    patient = find_patient_by_health_ids(health_ids)
    if patient is None and phone != PLACEHOLDER_PHONE_NUMBER:
        patient = Patient.objects.filter(phone_number=phone).first()
    if patient is None and name != UNIDENTIFIED_PATIENT:
        queryset = Patient.objects.filter(name__iexact=name)
        if date_of_birth is not None:
            queryset = queryset.filter(date_of_birth=date_of_birth)
        patient = queryset.filter(gender=gender).first() if gender else queryset.first()
    return patient


def find_or_create_patient(contract_attributes: dict, participant: dict | None, facility, user):
    """Reuse a patient matched by health id, phone or name (+ DOB/gender), else create one."""
    from care.emr.models import Patient
    from django.utils import timezone

    from care_ccn_be.services import facility as facilities
    from care_ccn_be.services import identifiers, mappers

    if not participant:
        return None
    attributes = participant.get("participantAttributes") or {}
    name = (participant.get("descriptor") or {}).get("name") or UNIDENTIFIED_PATIENT
    health_ids = mappers.extract_health_ids(participant)
    phone = resolve_phone(contract_attributes, participant)
    date_of_birth = mappers.extract_date_of_birth(participant)
    gender = mappers.map_gender(attributes.get("gender"))

    existing = _match_existing(health_ids, phone, name, gender, date_of_birth)
    if existing is not None:
        identifiers.upsert_health_ids(existing, health_ids)
        return existing

    patient = Patient(
        name=name,
        gender=gender,
        phone_number=phone,
        date_of_birth=date_of_birth,
        year_of_birth=None if date_of_birth else timezone.now().year - DEFAULT_REFERRAL_AGE_YEARS,
        geo_organization=facilities.default_geo_organization(facility),
        instance_identifiers=identifiers.instance_identifiers(health_ids),
        created_by=user,
        updated_by=user,
        extensions={"beckn": {"primaryLanguage": attributes.get("primaryLanguage"), "participant": participant}},
    )
    patient.save()
    identifiers.upsert_health_ids(patient, health_ids)
    return patient
