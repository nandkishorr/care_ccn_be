"""Care extension registrations: ``Patient.extensions["beckn"]`` keeps what a network patient carries that Care has
no column for (``primaryLanguage``, the source participant)."""

from care.emr.extensions.base import ExtensionResource, PlugExtension
from care.emr.registries.extensions.registry import ExtensionRegistry
from jsonschema import ValidationError, validate

EXTENSION_NAME = "beckn"
EXTENSION_VERSION = "1"

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


class PatientBecknExtension(BecknExtension):
    resource_type = ExtensionResource.patient
    schema = PATIENT_SCHEMA


def register() -> None:
    ExtensionRegistry.register(PatientBecknExtension())
