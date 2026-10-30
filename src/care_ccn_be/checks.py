from django.apps import apps
from django.core.checks import Warning, register

CORE_BECKN_APP = "care.beckn"


def core_beckn_installed() -> bool:
    return apps.is_installed(CORE_BECKN_APP)


@register()
def check_core_beckn_inactive(app_configs, **kwargs):
    """The plugin stays passive while Care core still ships its own Beckn app."""
    if not core_beckn_installed():
        return []
    return [
        Warning(
            "care_ccn_be is installed but inactive while care.beckn is installed.",
            hint="Disable the core Beckn app (BECKN_CORE_ENABLED=False) to activate the plugin.",
            id="care_ccn_be.W001",
        )
    ]
