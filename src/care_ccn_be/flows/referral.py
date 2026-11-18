from care_ccn_be import extensions
from care_ccn_be.context import ReferralContext
from care_ccn_be.flows.base import BecknFlow
from care_ccn_be.resources.beckn import BecknRequest, contract_attributes
from care_ccn_be.resources.beckn.health import HealthContract
from care_ccn_be.resources.catalog.referral import ReferralCatalogBuilder
from care_ccn_be.resources.referral.schema import HealthReferral
from care_ccn_be.resources.referral.spec import ReferralState


class ReferralFlow(BecknFlow):
    name = "referral"
    context_builder = ReferralContext
    state_model = ReferralState
    catalog_builder = ReferralCatalogBuilder

    @classmethod
    def match(cls, request: BecknRequest) -> int:
        attributes = contract_attributes(request)
        if isinstance(attributes, HealthReferral):
            return 30
        return 10 if cls._coordination_ref(attributes) else 0

    @classmethod
    def coordination_id(cls, request: BecknRequest) -> str:
        attributes = contract_attributes(request)
        if isinstance(attributes, HealthReferral) and attributes.coordination_id:
            return attributes.coordination_id
        found = cls._coordination_ref(attributes)
        if found:
            return found
        contract = getattr(getattr(request, "message", None), "contract", None)
        return getattr(contract, "id", None) or ""

    @staticmethod
    def _coordination_ref(attributes) -> str | None:
        """``coordinationRef`` is declared on ``HealthContract`` and ``HealthReferral``."""
        if isinstance(attributes, HealthContract | HealthReferral):
            return attributes.coordination_ref
        return None


extensions.register()
