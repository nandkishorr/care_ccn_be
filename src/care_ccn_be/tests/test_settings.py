import os
from unittest import mock

from django.test import SimpleTestCase, override_settings

from care_ccn_be.settings import DEFAULTS, plugin_settings


class PluginSettingsTests(SimpleTestCase):
    def setUp(self):
        plugin_settings.reload()
        self.addCleanup(plugin_settings.reload)

    def test_every_default_resolves(self):
        for name, default in DEFAULTS.items():
            self.assertEqual(getattr(plugin_settings, name), default, name)

    def test_unknown_setting_raises(self):
        with self.assertRaises(AttributeError):
            getattr(plugin_settings, "NOT_A_SETTING")  # noqa: B009

    def test_plugin_configs_override(self):
        with override_settings(PLUGIN_CONFIGS={"care_ccn_be": {"BECKN_BAP_ID": "bap.example"}}):
            self.assertEqual(plugin_settings.BECKN_BAP_ID, "bap.example")

    def test_environment_override_is_cast_to_default_type(self):
        with mock.patch.dict(os.environ, {"BECKN_HTTP_TIMEOUT": "30", "BECKN_ENABLED_FLOWS": "referral"}):
            self.assertEqual(plugin_settings.BECKN_HTTP_TIMEOUT, 30)
            self.assertEqual(plugin_settings.BECKN_ENABLED_FLOWS, ["referral"])

    def test_plugin_configs_beat_environment(self):
        with (
            mock.patch.dict(os.environ, {"BECKN_BAP_ID": "from-env"}),
            override_settings(PLUGIN_CONFIGS={"care_ccn_be": {"BECKN_BAP_ID": "from-config"}}),
        ):
            self.assertEqual(plugin_settings.BECKN_BAP_ID, "from-config")
