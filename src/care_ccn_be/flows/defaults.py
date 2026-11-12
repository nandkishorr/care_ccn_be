from care_ccn_be.protocol.context import Endpoint

REFERRAL_DEFAULTS = {
    "BECKN_CC_BPP_ID": "",
    "BECKN_CC_BPP_URI": "",
    "BECKN_COORDINATOR_NAME": "Care Coordination Desk",
    "BECKN_COORDINATOR_ID": "",
    "BECKN_COORDINATOR_TARGET_SERVICE_TYPES": ["CONSULTATION", "INVESTIGATION", "DISPENSING"],
    "BECKN_COORDINATOR_PROGRAMMES": ["PMJAY"],
    "BECKN_COORDINATOR_DISTRICT": "",
    "BECKN_REFERRAL_TYPES": ["consultation", "lab"],
    "BECKN_REFERRAL_STATUS": "pending",
}


def cc_endpoint() -> Endpoint:
    from care_ccn_be.settings import plugin_settings

    return Endpoint(plugin_settings.BECKN_CC_BPP_ID, plugin_settings.BECKN_CC_BPP_URI)
