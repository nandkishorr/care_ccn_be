"""NFH health ids (ABHA, HFR, MRN, ...) as Care ``PatientIdentifier`` rows.

An instance-level ``PatientIdentifierConfig`` is created per health-id system on first use.
"""

from care.emr.models.patient import PatientIdentifier, PatientIdentifierConfig
from care.emr.resources.patient_identifier.spec import (
    IdentifierConfig,
    PatientIdentifierStatus,
    PatientIdentifierUse,
)

HEALTH_ID_SYSTEMS = {
    "ABHA": ("system.care.ohc.network/abha-number", "ABHA Number"),
    "HFR": ("system.care.ohc.network/hfr-id", "HFR ID"),
    "MRN": ("system.care.ohc.network/mrn", "Medical Record Number"),
}
SLUG_TO_HEALTH_ID_SYSTEM = {slug: system for system, (slug, _display) in HEALTH_ID_SYSTEMS.items()}


def system_slug(system: str | None) -> tuple[str, str]:
    key = (system or "").upper()
    if key in HEALTH_ID_SYSTEMS:
        return HEALTH_ID_SYSTEMS[key]
    slug = (system or "external").strip().lower().replace(" ", "-")
    return f"system.care.ohc.network/{slug}", system or "External Identifier"


def get_or_create_config(slug: str, display: str) -> PatientIdentifierConfig:
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


def upsert_health_ids(patient, health_ids: list[dict]) -> None:
    """Create/update identifier rows and rebuild ``Patient.instance_identifiers`` (the JSON Care's API reads)."""
    touched = False
    for item in health_ids or []:
        value = item.get("value")
        if not value:
            continue
        touched = True
        config = get_or_create_config(*system_slug(item.get("system")))
        identifier = PatientIdentifier.objects.filter(patient=patient, config=config).first()
        if identifier is None:
            PatientIdentifier.objects.create(patient=patient, config=config, value=value)
        elif identifier.value != value:
            identifier.value = value
            identifier.save()
    if touched:
        patient.build_instance_identifiers()
        patient.save()


def instance_identifiers(health_ids: list[dict]) -> list[dict]:
    entries = []
    for item in health_ids or []:
        if item.get("value"):
            config = get_or_create_config(*system_slug(item.get("system")))
            entries.append({"config": str(config.external_id), "value": item["value"]})
    return entries


def health_ids_from_patient(patient) -> list[dict]:
    if patient is None or not getattr(patient, "pk", None):
        return []
    results = []
    for identifier in PatientIdentifier.objects.filter(patient=patient).select_related("config"):
        system = SLUG_TO_HEALTH_ID_SYSTEM.get((identifier.config.config or {}).get("system"))
        if system and identifier.value:
            results.append({"system": system, "value": identifier.value})
    return results


def find_patient_by_health_ids(health_ids: list[dict]):
    for item in health_ids or []:
        if not item.get("value"):
            continue
        slug, _ = system_slug(item.get("system"))
        identifier = (
            PatientIdentifier.objects.filter(value=item["value"], config__config__system=slug)
            .select_related("patient")
            .first()
        )
        if identifier is not None:
            return identifier.patient
    return None
