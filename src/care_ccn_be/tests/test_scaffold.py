from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from care_ccn_be import checks


@override_settings(ROOT_URLCONF="care_ccn_be.tests.framework.urls")
class HealthTests(SimpleTestCase):
    def test_health_endpoint(self):
        response = self.client.get(reverse("care_ccn_be-health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"OK")


class CoreBecknCheckTests(SimpleTestCase):
    def test_silent_when_core_beckn_is_absent(self):
        with mock.patch.object(checks, "core_beckn_installed", return_value=False):
            self.assertEqual(checks.check_core_beckn_inactive(None), [])

    def test_w001_when_core_beckn_is_installed(self):
        with mock.patch.object(checks, "core_beckn_installed", return_value=True):
            messages = checks.check_core_beckn_inactive(None)
        self.assertEqual([m.id for m in messages], ["care_ccn_be.W001"])
