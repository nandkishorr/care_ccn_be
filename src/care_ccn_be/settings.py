from typing import Any

import environ
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver
from rest_framework.settings import perform_import

from care_ccn_be.adapter import DEFAULTS as ADAPTER_DEFAULTS
from care_ccn_be.apps import PLUGIN_NAME
from care_ccn_be.flows.defaults import REFERRAL_DEFAULTS

env = environ.Env()


class PluginSettings:  # pragma: no cover
    """
    A settings object that allows plugin settings to be accessed as
    properties. For example:

        from care_ccn_be.settings import plugin_settings
        print(plugin_settings.BECKN_BAP_ID)

    Lookup order: ``PLUGIN_CONFIGS["care_ccn_be"]`` -> environment variable -> default.
    Any setting listed in ``import_strings`` is resolved from a dotted path to the object.
    """

    def __init__(
        self,
        plugin_name: str | None = None,
        defaults: dict | None = None,
        import_strings: set | None = None,
        required_settings: set | None = None,
    ) -> None:
        if not plugin_name:
            raise ValueError("Plugin name must be provided")
        self.plugin_name = plugin_name
        self.defaults = defaults or {}
        self.import_strings = import_strings or set()
        self.required_settings = required_settings or set()
        self._cached_attrs = set()
        self.validate()

    def __getattr__(self, attr) -> Any:
        if attr not in self.defaults:
            raise AttributeError(f"Invalid setting: '{attr}'")

        val = self.defaults[attr]
        try:
            val = self.user_settings[attr]
        except KeyError:
            try:
                val = env(attr, cast=type(val))
            except environ.ImproperlyConfigured:
                pass

        if attr in self.import_strings:
            val = perform_import(val, attr)

        self._cached_attrs.add(attr)
        setattr(self, attr, val)
        return val

    @property
    def user_settings(self) -> dict:
        if not hasattr(self, "_user_settings"):
            self._user_settings = getattr(settings, "PLUGIN_CONFIGS", {}).get(self.plugin_name, {})
        return self._user_settings

    def validate(self) -> None:
        for setting in self.required_settings:
            if not getattr(self, setting):
                raise ImproperlyConfigured(
                    f'The "{setting}" setting is required. '
                    f'Please set the "{setting}" in the environment or the {PLUGIN_NAME} plugin config.'
                )

    def reload(self) -> None:
        """Drop cached values so they are recomputed on next access."""
        for attr in self._cached_attrs:
            delattr(self, attr)
        self._cached_attrs.clear()
        if hasattr(self, "_user_settings"):
            delattr(self, "_user_settings")


REQUIRED_SETTINGS: set[str] = set()

DEFAULTS = {
    # identity and network
    "BECKN_NETWORK_ID": "",
    "BECKN_VERSION": "2.0.0",
    **ADAPTER_DEFAULTS,
    "BECKN_SYSTEM_USERNAME": "beckn_system",
    **REFERRAL_DEFAULTS,
    # flows, delivery and security
    "BECKN_ENABLED_FLOWS": ["referral", "appointment"],
    "BECKN_HTTP_TIMEOUT": 10,
    "BECKN_MAX_RETRIES": 5,
    "BECKN_MESSAGE_RETENTION_DAYS": 90,
    "BECKN_ALLOWED_IPS": [],
    "BECKN_SHARED_SECRET": "",
    "BECKN_CELERY_QUEUE": "",
    "BECKN_CALLBACK_REUSE_MESSAGE_ID": True,
}

plugin_settings = PluginSettings(PLUGIN_NAME, defaults=DEFAULTS, required_settings=REQUIRED_SETTINGS)


@receiver(setting_changed)
def reload_plugin_settings(*args, **kwargs) -> None:
    setting = kwargs["setting"]
    if setting == "PLUGIN_CONFIGS":
        plugin_settings.reload()
