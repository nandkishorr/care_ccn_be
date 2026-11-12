from care.emr.registries.extensions.registry import ExtensionRegistry
from django.test import SimpleTestCase


class ExtensionTests(SimpleTestCase):
    def extension(self, resource):
        return ExtensionRegistry.get_extension_obj(resource, "beckn")

    def test_only_the_patient_extension_is_registered(self):
        extension = self.extension("patient")
        self.assertIsNotNone(extension)
        self.assertEqual(extension.extension_version, "1")
        self.assertIsNone(self.extension("resource_request"))

    def test_patient_extension_is_permissive(self):
        extension = self.extension("patient")
        self.assertEqual(extension.validate({"primaryLanguage": "en"}), {"primaryLanguage": "en"})
