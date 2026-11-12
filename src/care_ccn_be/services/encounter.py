"""Encounter lookups shared by flows. Flows go through here instead of importing Care models directly."""

from rest_framework.exceptions import NotFound, ValidationError

ENCOUNTER_PATIENT_MISMATCH = "Patient does not match the encounter patient"
ENCOUNTER_FACILITY_MISMATCH = "Facility does not match the encounter facility"


def by_external_id(external_id):
    from care.emr.models import Encounter

    if not external_id:
        return None
    return Encounter.objects.select_related("patient", "facility").filter(external_id=external_id).first()


def get_by_external_id(external_id):
    encounter = by_external_id(external_id)
    if encounter is None:
        raise NotFound("Unknown encounter")
    return encounter


def validate(external_id, patient_id=None, facility_id=None):
    """The encounter ``external_id``, checked to belong to the given patient and facility (external ids)."""
    encounter = get_by_external_id(external_id)
    if patient_id and str(encounter.patient.external_id) != str(patient_id):
        raise ValidationError(ENCOUNTER_PATIENT_MISMATCH)
    if facility_id and str(encounter.facility.external_id) != str(facility_id):
        raise ValidationError(ENCOUNTER_FACILITY_MISMATCH)
    return encounter
