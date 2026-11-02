"""Authorization for the network ``ReferralAvailability`` rows (``entity_type=external``).

Care hides ``external`` availabilities from everyone, because only plugins create them. A network availability is
public and active by construction, so it is made listable and viewable like any public availability. Everything else
is Care's own ``ReferralAccess``.
"""

from care.security.authorization.base import AuthorizationController
from care.security.authorization.referral import ReferralAccess

from care_ccn_be.services import availability as availabilities


class BecknReferralAccess(ReferralAccess):
    def can_view_referral_availability(self, user, availability):
        if availabilities.is_network(availability):
            return bool(user.is_authenticated) and availability.status == "active" and availability.is_public
        return super().can_view_referral_availability(user, availability)

    def get_accessible_referral_availabilities(self, qs, user, entity_type):
        if entity_type == "external":
            if not user.is_authenticated:
                return qs.none()
            return qs.filter(entity_type="external", status="active", is_public=True)
        return super().get_accessible_referral_availabilities(qs, user, entity_type)


def register() -> None:
    if BecknReferralAccess not in AuthorizationController.override_authz_controllers:
        AuthorizationController.register_override_controller(BecknReferralAccess)
        AuthorizationController.cache = {"actions": {}, "queries": {}}
