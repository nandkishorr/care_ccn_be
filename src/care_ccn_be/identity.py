from care_ccn_be import adapter
from care_ccn_be.protocol.context import NetworkIdentity
from care_ccn_be.settings import plugin_settings


def identity_from_settings() -> NetworkIdentity:
    return NetworkIdentity(
        network_id=plugin_settings.BECKN_NETWORK_ID,
        version=plugin_settings.BECKN_VERSION,
        bap=adapter.bap_endpoint(),
        bpp=adapter.bpp_endpoint(),
    )
