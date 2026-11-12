import os
from unittest import mock

from django.test import SimpleTestCase, override_settings

from care_ccn_be import adapter
from care_ccn_be.adapter import DEFAULTS as ADAPTER_DEFAULTS
from care_ccn_be.flows.defaults import REFERRAL_DEFAULTS, cc_endpoint
from care_ccn_be.identity import identity_from_settings
from care_ccn_be.protocol.context import Endpoint
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

    def test_adapter_defaults_are_included_in_plugin_defaults(self):
        for name, default in ADAPTER_DEFAULTS.items():
            self.assertEqual(DEFAULTS[name], default)

    def test_referral_defaults_are_included_in_plugin_defaults(self):
        for name, default in REFERRAL_DEFAULTS.items():
            self.assertEqual(DEFAULTS[name], default)

    def test_adapter_configuration_reloads_with_plugin_settings(self):
        for suffix in ("one", "two"):
            config = {
                "BECKN_BAP_ID": f"bap.{suffix}",
                "BECKN_BAP_URI": f"http://bap.{suffix}",
                "BECKN_BAP_CALLER_URL": f"http://bap.{suffix}/bap/caller",
                "BECKN_BPP_ID": f"bpp.{suffix}",
                "BECKN_BPP_URI": f"http://bpp.{suffix}",
                "BECKN_BPP_CALLER_URL": f"http://bpp.{suffix}/bpp/caller",
                "BECKN_CC_BPP_ID": "remote.cc",
                "BECKN_CC_BPP_URI": "http://remote.cc",
            }
            with override_settings(PLUGIN_CONFIGS={"care_ccn_be": config}):
                self.assertEqual(adapter.bap_endpoint(), Endpoint(f"bap.{suffix}", f"http://bap.{suffix}"))
                self.assertEqual(adapter.bpp_endpoint(), Endpoint(f"bpp.{suffix}", f"http://bpp.{suffix}"))
                self.assertEqual(adapter.bap_caller_url(), config["BECKN_BAP_CALLER_URL"])
                self.assertEqual(adapter.bpp_caller_url(), config["BECKN_BPP_CALLER_URL"])
                identity = identity_from_settings()
                self.assertEqual(identity.bap, adapter.bap_endpoint())
                self.assertEqual(identity.bpp, adapter.bpp_endpoint())
                self.assertEqual(cc_endpoint(), Endpoint("remote.cc", "http://remote.cc"))

    def test_catalog_publish_base_preserves_onix_routing(self):
        cases = (
            ("", "http://onix/bpp/caller/", "http://onix"),
            ("http://publish/catalog/publish/", "http://onix/bpp/caller", "http://publish"),
            ("http://publish/", "", "http://publish"),
            ("", "", ""),
            (None, None, ""),
        )
        for explicit, caller, expected in cases:
            with (
                self.subTest(explicit=explicit, caller=caller),
                override_settings(
                    PLUGIN_CONFIGS={
                        "care_ccn_be": {"BECKN_CATALOG_PUBLISH_URL": explicit, "BECKN_BPP_CALLER_URL": caller}
                    }
                ),
            ):
                self.assertEqual(adapter.catalog_publish_base(), expected)
