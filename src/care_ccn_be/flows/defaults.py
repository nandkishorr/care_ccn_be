from care_ccn_be.protocol.context import Endpoint

REFERRAL_DEFAULTS = {
    "BECKN_CC_BPP_ID": "",
    "BECKN_CC_BPP_URI": "",
    "BECKN_COORDINATOR_NAME": "Care Coordination Desk",
    "BECKN_COORDINATOR_ID": "",
    "BECKN_COORDINATOR_TARGET_SERVICE_TYPES": ["CONSULTATION", "INVESTIGATION", "DISPENSING"],
    "BECKN_COORDINATOR_PROGRAMMES": ["PMJAY"],
    "BECKN_COORDINATOR_DISTRICT": "",
    # ServiceCoordinationOffer of every offer in a facility's coordinator catalog
    "BECKN_COORDINATOR_SLA_HOURS": {"ROUTINE": 168, "URGENT": 48, "EMERGENCY": 1},
    "BECKN_COORDINATOR_RETRY_POLICY": "ONE_SILENT_RETRY",
    "BECKN_COORDINATOR_HANDOFF_SUPPORTED": True,
    "BECKN_COORDINATOR_MAX_ACTIVE_REFERRALS": 500,
    "BECKN_REFERRAL_TYPES": ["consultation", "lab"],
    "BECKN_REFERRAL_STATUS": "pending",
    # publish the facility's coordinator catalog when one of its receiving ReferralAvailabilities changes
    "BECKN_REFERRAL_CATALOG_AUTO_PUBLISH": True,
}


def cc_endpoint() -> Endpoint:
    from care_ccn_be.settings import plugin_settings

    return Endpoint(plugin_settings.BECKN_CC_BPP_ID, plugin_settings.BECKN_CC_BPP_URI)
