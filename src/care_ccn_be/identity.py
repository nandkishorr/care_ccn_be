from care_ccn_be.protocol.context import Endpoint, NetworkIdentity
from care_ccn_be.settings import plugin_settings


def identity_from_settings() -> NetworkIdentity:
    return NetworkIdentity(
        network_id=plugin_settings.BECKN_NETWORK_ID,
        version=plugin_settings.BECKN_VERSION,
        bap=Endpoint(plugin_settings.BECKN_BAP_ID, plugin_settings.BECKN_BAP_URI),
        bpp=Endpoint(plugin_settings.BECKN_BPP_ID, plugin_settings.BECKN_BPP_URI),
    )


def cc_endpoint() -> Endpoint:
    """The configured coordination center, the default BPP for referrals."""
    return Endpoint(plugin_settings.BECKN_CC_BPP_ID, plugin_settings.BECKN_CC_BPP_URI)
