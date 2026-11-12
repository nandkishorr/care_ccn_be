"""Care's BAP and BPP identity, caller, and catalog-publishing configuration."""

from care_ccn_be.protocol.context import Endpoint

DEFAULTS = {
    "BECKN_BAP_ID": "",
    "BECKN_BAP_URI": "",
    "BECKN_BAP_CALLER_URL": "",
    "BECKN_BPP_ID": "",
    "BECKN_BPP_URI": "",
    "BECKN_BPP_CALLER_URL": "",
    "BECKN_CATALOG_PUBLISH_URL": "",
}

CATALOG_PUBLISH = "catalog/publish"
BPP_CALLER_PATH = "/bpp/caller"


def _settings():
    from care_ccn_be.settings import plugin_settings

    return plugin_settings


def bap_endpoint() -> Endpoint:
    settings = _settings()
    return Endpoint(settings.BECKN_BAP_ID, settings.BECKN_BAP_URI)


def bap_caller_url() -> str:
    return _settings().BECKN_BAP_CALLER_URL


def bpp_endpoint() -> Endpoint:
    settings = _settings()
    return Endpoint(settings.BECKN_BPP_ID, settings.BECKN_BPP_URI)


def bpp_caller_url() -> str:
    return _settings().BECKN_BPP_CALLER_URL


def catalog_publish_base() -> str:
    """ONIX's publish module is at the adapter root, not under the BPP caller."""
    explicit = (_settings().BECKN_CATALOG_PUBLISH_URL or "").rstrip("/")
    if explicit:
        return explicit.removesuffix(f"/{CATALOG_PUBLISH}")
    return (bpp_caller_url() or "").rstrip("/").removesuffix(BPP_CALLER_PATH)
