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


APPOINTMENT_HEALTH_ID_SYSTEMS = {"ABHA": ("system.care.ohc.network/abha-number", "ABHA Number")}
APPOINTMENT_NFH_TO_CARE_GENDER = {"MALE": "male", "FEMALE": "female"}
APPOINTMENT_CARE_TO_NFH_GENDER = {"male": "MALE", "female": "FEMALE"}
APPOINTMENT_OTHER_NFH_GENDER = "OTHER"
APPOINTMENT_NON_BINARY = "non_binary"
DEFAULT_APPOINTMENT_AGE_YEARS = 35


def appointment_system_slug(system: str | None) -> tuple[str, str]:
    key = (system or "").upper()
    if key in APPOINTMENT_HEALTH_ID_SYSTEMS:
        return APPOINTMENT_HEALTH_ID_SYSTEMS[key]
    slug = (system or "external").strip().lower().replace(" ", "-")
    return f"system.care.ohc.network/{slug}", system or "External Identifier"


def _appointment_identifier_config(slug: str, display: str):
    from care.emr.models.patient import PatientIdentifierConfig
    from care.emr.resources.patient_identifier.spec import (
        IdentifierConfig,
        PatientIdentifierStatus,
        PatientIdentifierUse,
    )

    config = PatientIdentifierConfig.objects.filter(facility__isnull=True, config__system=slug).first()
    if config is None:
        config = PatientIdentifierConfig.objects.create(
            facility=None,
            status=PatientIdentifierStatus.active.value,
            config=IdentifierConfig(
                use=PatientIdentifierUse.secondary,
                system=slug,
                required=False,
                unique=False,
                regex="",
                display=display,
                auto_maintained=True,
            ).model_dump(mode="json"),
        )
    return config


def _appointment_health_ids_of(patient) -> list[dict]:
    from care.emr.models.patient import PatientIdentifier

    slug_to_system = {slug: system for system, (slug, _) in APPOINTMENT_HEALTH_ID_SYSTEMS.items()}
    ids = []
    for identifier in PatientIdentifier.objects.filter(patient=patient).select_related("config"):
        system = slug_to_system.get((identifier.config.config or {}).get("system"))
        if system and identifier.value:
            ids.append({"system": system, "value": identifier.value})
    return ids


def appointment_patient_info(patient):
    from care_ccn_be.resources.builders.appointment import PatientInfo

    abha = next((item["value"] for item in _appointment_health_ids_of(patient) if item["system"] == "ABHA"), None)
    return PatientInfo(
        name=patient.name,
        gender=APPOINTMENT_CARE_TO_NFH_GENDER.get(patient.gender, APPOINTMENT_OTHER_NFH_GENDER),
        dob=patient.date_of_birth,
        language=((patient.extensions or {}).get("beckn") or {}).get("primaryLanguage"),
        abha=abha,
    )


def _appointment_participant_health_ids(attributes) -> list[dict]:
    return [{"system": item.system, "value": item.value} for item in getattr(attributes, "health_ids", []) or []]


def _upsert_appointment_health_ids(patient, health_ids: list[dict]) -> None:
    from care.emr.models.patient import PatientIdentifier

    touched = False
    for item in health_ids:
        config = _appointment_identifier_config(*appointment_system_slug(item["system"]))
        identifier = PatientIdentifier.objects.filter(patient=patient, config=config).first()
        if identifier is None:
            PatientIdentifier.objects.create(patient=patient, config=config, value=item["value"])
        elif identifier.value != item["value"]:
            identifier.value = item["value"]
            identifier.save()
        touched = True
    if touched:
        patient.build_instance_identifiers()
        patient.save()


def _match_appointment_patient(health_ids: list[dict], name: str, gender: str, date_of_birth):
    from care.emr.models import Patient
    from care.emr.models.patient import PatientIdentifier

    for item in health_ids:
        slug, _ = appointment_system_slug(item["system"])
        identifier = (
            PatientIdentifier.objects.filter(value=item["value"], config__config__system=slug)
            .select_related("patient")
            .first()
        )
        if identifier is not None:
            return identifier.patient
    if name != UNIDENTIFIED_PATIENT:
        queryset = Patient.objects.filter(name__iexact=name, gender=gender)
        if date_of_birth is not None:
            queryset = queryset.filter(date_of_birth=date_of_birth)
        return queryset.first()
    return None


def find_or_create_appointment_patient(contract, facility, user):
    """Resolve the appointment PATIENT participant by health ID, then demographics."""
    from care.emr.models import Patient
    from django.utils import timezone

    from care_ccn_be.resources.appointment.schema import PATIENT

    participant = contract.participant(PATIENT)
    if participant is None:
        return None
    attributes = participant.participant_attributes
    name = (participant.descriptor.name if participant.descriptor else None) or UNIDENTIFIED_PATIENT
    gender = APPOINTMENT_NFH_TO_CARE_GENDER.get(
        (getattr(attributes, "gender", None) or "").upper(), APPOINTMENT_NON_BINARY
    )
    date_of_birth = getattr(attributes, "date_of_birth", None)
    health_ids = _appointment_participant_health_ids(attributes)

    patient = _match_appointment_patient(health_ids, name, gender, date_of_birth)
    if patient is None:
        patient = Patient.objects.create(
            name=name,
            gender=gender,
            phone_number=PLACEHOLDER_PHONE_NUMBER,
            date_of_birth=date_of_birth,
            year_of_birth=None if date_of_birth else timezone.now().year - DEFAULT_APPOINTMENT_AGE_YEARS,
            geo_organization=facility.geo_organization,
            created_by=user,
            updated_by=user,
            extensions={"beckn": {"primaryLanguage": getattr(attributes, "primary_language", None)}},
        )
    _upsert_appointment_health_ids(patient, health_ids)
    return patient
