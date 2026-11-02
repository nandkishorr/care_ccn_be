"""Care extension registrations. The referral target now lives on the network ``ReferralAvailability``
(``services/availability.py``); the ResourceRequest extension stays registered only so legacy rows that still carry
``extensions.beckn`` keep validating and serializing."""

from care.emr.extensions.base import ExtensionResource, PlugExtension
from care.emr.registries.extensions.registry import ExtensionRegistry
from jsonschema import ValidationError, validate

EXTENSION_NAME = "beckn"
EXTENSION_VERSION = "1"

RESOURCE_REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {
            "type": "object",
            "properties": {
                "bppId": {"type": "string"},
                "bppUri": {"type": "string"},
                "assignedFacilityId": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": True,
}

PATIENT_SCHEMA = {"type": "object", "additionalProperties": True}


class BecknExtension(PlugExtension):
    extension_name = EXTENSION_NAME
    extension_version = EXTENSION_VERSION
    schema: dict = {}

    def get_write_schema(self):
        return self.schema

    def get_read_schema(self):
        return self.schema

    def get_retrieve_schema(self):
        return self.schema

    def validate(self, data, resource=None):
        try:
            validate(data, self.schema)
        except ValidationError as exc:
            raise ValueError(f"Invalid beckn extension: {exc.message}") from exc
        return data


class ResourceRequestBecknExtension(BecknExtension):
    resource_type = ExtensionResource.resource_request
    schema = RESOURCE_REQUEST_SCHEMA


class PatientBecknExtension(BecknExtension):
    resource_type = ExtensionResource.patient
    schema = PATIENT_SCHEMA


def register() -> None:
    for extension in (ResourceRequestBecknExtension(), PatientBecknExtension()):
        ExtensionRegistry.register(extension)
