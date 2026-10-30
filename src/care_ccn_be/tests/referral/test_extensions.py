from care.emr.registries.extensions.registry import ExtensionRegistry
from django.test import SimpleTestCase


class ExtensionTests(SimpleTestCase):
    def extension(self, resource):
        return ExtensionRegistry.get_extension_obj(resource, "beckn")

    def test_extensions_are_registered_for_resource_requests_and_patients(self):
        for resource in ("resource_request", "patient"):
            extension = self.extension(resource)
            self.assertIsNotNone(extension)
            self.assertEqual(extension.extension_version, "1")

    def test_target_is_validated(self):
        extension = self.extension("resource_request")
        data = {"target": {"bppId": "b", "bppUri": "u", "assignedFacilityId": "f"}}
        self.assertEqual(extension.validate(data), data)
        with self.assertRaises(ValueError):
            extension.validate({"target": {"bppId": 1}})
        with self.assertRaises(ValueError):
            extension.validate({"target": {"unknown": "x"}})

    def test_patient_extension_is_permissive(self):
        extension = self.extension("patient")
        self.assertEqual(extension.validate({"primaryLanguage": "en"}), {"primaryLanguage": "en"})
