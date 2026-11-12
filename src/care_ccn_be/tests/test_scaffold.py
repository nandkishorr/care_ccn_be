from django.test import SimpleTestCase, override_settings
from django.urls import reverse


@override_settings(ROOT_URLCONF="care_ccn_be.tests.framework.urls")
class HealthTests(SimpleTestCase):
    def test_health_endpoint(self):
        response = self.client.get(reverse("care_ccn_be-health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"OK")
