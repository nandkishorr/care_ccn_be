"""Patient lookups shared by flows. Flows go through here instead of importing Care models directly."""

from rest_framework.exceptions import NotFound


def get_by_external_id(external_id):
    from care.emr.models import Patient

    patient = Patient.objects.filter(external_id=external_id).first()
    if patient is None:
        raise NotFound("Unknown patient")
    return patient
