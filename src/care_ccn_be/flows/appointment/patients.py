"""Patient resolution for the appointment flow: a Care ``Patient`` <-> the NFH PATIENT participant."""

from django.utils import timezone

from care_ccn_be.flows.appointment.builders import PatientInfo
from care_ccn_be.flows.appointment.schema import PATIENT

PLACEHOLDER_PHONE_NUMBER = "0000000000"
DEFAULT_AGE_YEARS = 35
UNIDENTIFIED_PATIENT = "Unidentified Patient"
HEALTH_ID_SYSTEMS = {"ABHA": ("system.care.ohc.network/abha-number", "ABHA Number")}
NFH_TO_CARE_GENDER = {"MALE": "male", "FEMALE": "female"}
CARE_TO_NFH_GENDER = {"male": "MALE", "female": "FEMALE"}
OTHER_NFH_GENDER = "OTHER"
NON_BINARY = "non_binary"


def system_slug(system: str | None) -> tuple[str, str]:
    key = (system or "").upper()
    if key in HEALTH_ID_SYSTEMS:
        return HEALTH_ID_SYSTEMS[key]
    slug = (system or "external").strip().lower().replace(" ", "-")
    return f"system.care.ohc.network/{slug}", system or "External Identifier"


def _config(slug: str, display: str):
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


def health_ids_of(patient) -> list[dict]:
    from care.emr.models.patient import PatientIdentifier

    slug_to_system = {slug: system for system, (slug, _) in HEALTH_ID_SYSTEMS.items()}
    ids = []
    for identifier in PatientIdentifier.objects.filter(patient=patient).select_related("config"):
        system = slug_to_system.get((identifier.config.config or {}).get("system"))
        if system and identifier.value:
            ids.append({"system": system, "value": identifier.value})
    return ids


def patient_info(patient) -> PatientInfo:
    abha = next((item["value"] for item in health_ids_of(patient) if item["system"] == "ABHA"), None)
    return PatientInfo(
        name=patient.name,
        gender=CARE_TO_NFH_GENDER.get(patient.gender, OTHER_NFH_GENDER),
        dob=patient.date_of_birth,
        language=((patient.extensions or {}).get("beckn") or {}).get("primaryLanguage"),
        abha=abha,
    )


def _participant_health_ids(attributes) -> list[dict]:
    return [{"system": item.system, "value": item.value} for item in getattr(attributes, "health_ids", []) or []]


def _upsert_health_ids(patient, health_ids: list[dict]) -> None:
    from care.emr.models.patient import PatientIdentifier

    touched = False
    for item in health_ids:
        config = _config(*system_slug(item["system"]))
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


def _match(health_ids: list[dict], name: str, gender: str, date_of_birth):
    from care.emr.models import Patient
    from care.emr.models.patient import PatientIdentifier

    for item in health_ids:
        slug, _ = system_slug(item["system"])
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


def find_or_create_patient(contract, facility, user):
    """The Care patient for the contract's PATIENT participant (matched by health id, then demographics)."""
    from care.emr.models import Patient

    participant = contract.participant(PATIENT)
    if participant is None:
        return None
    attributes = participant.participant_attributes
    name = (participant.descriptor.name if participant.descriptor else None) or UNIDENTIFIED_PATIENT
    gender = NFH_TO_CARE_GENDER.get((getattr(attributes, "gender", None) or "").upper(), NON_BINARY)
    date_of_birth = getattr(attributes, "date_of_birth", None)
    health_ids = _participant_health_ids(attributes)

    patient = _match(health_ids, name, gender, date_of_birth)
    if patient is None:
        patient = Patient.objects.create(
            name=name,
            gender=gender,
            phone_number=PLACEHOLDER_PHONE_NUMBER,
            date_of_birth=date_of_birth,
            year_of_birth=None if date_of_birth else timezone.now().year - DEFAULT_AGE_YEARS,
            geo_organization=facility.geo_organization,
            created_by=user,
            updated_by=user,
            extensions={"beckn": {"primaryLanguage": getattr(attributes, "primary_language", None)}},
        )
    _upsert_health_ids(patient, health_ids)
    return patient
