from care_ccn_be import extensions
from care_ccn_be.flows.base import BecknFlow
from care_ccn_be.flows.referral import lifecycle
from care_ccn_be.flows.referral.bpp import ReferralBppService
from care_ccn_be.flows.referral.callbacks import ReferralCallbacks
from care_ccn_be.flows.referral.schema import DISCOVER_SCHEMA_CONTEXT, HealthReferral
from care_ccn_be.flows.referral.specs import ReferralState
from care_ccn_be.flows.registry import FlowRegistry
from care_ccn_be.identity import cc_endpoint
from care_ccn_be.protocol.context import ContextBuilder, Endpoint
from care_ccn_be.protocol.schema import BecknRequest, contract_attributes


class ReferralContext(ContextBuilder):
    discover_schema_context = DISCOVER_SCHEMA_CONTEXT

    def default_bpp(self) -> Endpoint:
        return cc_endpoint()


@FlowRegistry.register
class ReferralFlow(BecknFlow):
    name = "referral"
    aliases = ("consultation", "coordination")
    legacy_service_type = "consultation"
    context_builder = ReferralContext
    state_model = ReferralState
    bap_viewset = "care_ccn_be.flows.referral.bap.ReferralBapViewSet"
    bap_callbacks = ReferralCallbacks
    bpp_service = ReferralBppService

    @classmethod
    def match(cls, request: BecknRequest) -> int:
        attributes = contract_attributes(request)
        if isinstance(attributes, HealthReferral):
            return 30
        return 10 if attributes is not None and cls._extra(attributes, "coordinationRef") else 0

    @classmethod
    def coordination_id(cls, request: BecknRequest) -> str:
        attributes = contract_attributes(request)
        contract = getattr(getattr(request, "message", None), "contract", None)
        if attributes is not None:
            found = getattr(attributes, "coordination_id", None) or cls._extra(attributes, "coordinationRef")
            if found:
                return found
        return getattr(contract, "id", None) or ""

    @staticmethod
    def _extra(attributes, key: str):
        return (getattr(attributes, "model_extra", None) or {}).get(key)

    @classmethod
    def poll_extra(cls, txn) -> dict:
        return {"resourceRequestId": lifecycle.origin_request_id(txn)}


extensions.register()
