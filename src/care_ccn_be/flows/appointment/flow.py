from care_ccn_be.flows.appointment.bpp import AppointmentBppService
from care_ccn_be.flows.appointment.callbacks import AppointmentCallbacks
from care_ccn_be.flows.appointment.catalog import AppointmentCatalogBuilder
from care_ccn_be.flows.appointment.context import AppointmentContextBuilder
from care_ccn_be.flows.appointment.specs import AppointmentState
from care_ccn_be.flows.base import BecknFlow
from care_ccn_be.flows.registry import FlowRegistry
from care_ccn_be.protocol.actions import DISCOVER
from care_ccn_be.protocol.schema import BecknRequest, contract_attributes
from care_ccn_be.protocol.schema.health import HealthContract

HEALTH_CONTRACT_SCORE = 20
HEALTH_DISCOVER_SCORE = 10


@FlowRegistry.register
class AppointmentFlow(BecknFlow):
    name = "appointment"
    legacy_service_type = "appointment"
    context_builder = AppointmentContextBuilder
    state_model = AppointmentState
    bap_viewset = "care_ccn_be.flows.appointment.bap.AppointmentBapViewSet"
    bap_callbacks = AppointmentCallbacks
    bpp_service = AppointmentBppService
    catalog_builder = AppointmentCatalogBuilder

    @classmethod
    def match(cls, request: BecknRequest) -> int:
        context = request.context
        if context.action == DISCOVER:
            wanted = AppointmentContextBuilder.discover_schema_context
            return HEALTH_DISCOVER_SCORE if wanted in (context.schema_context or []) else 0
        return HEALTH_CONTRACT_SCORE if isinstance(contract_attributes(request), HealthContract) else 0

    @classmethod
    def coordination_id(cls, request: BecknRequest) -> str:
        attributes = contract_attributes(request)
        extra = (getattr(attributes, "model_extra", None) or {}) if attributes is not None else {}
        return str(extra.get("coordinationRef") or "")
