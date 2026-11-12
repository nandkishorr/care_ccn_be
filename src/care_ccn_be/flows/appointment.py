from care_ccn_be.context import AppointmentContextBuilder
from care_ccn_be.flows.base import BecknFlow
from care_ccn_be.protocol.actions import DISCOVER
from care_ccn_be.resources.appointment.spec import AppointmentState
from care_ccn_be.resources.beckn import BecknRequest, contract_attributes
from care_ccn_be.resources.beckn.health import HealthContract
from care_ccn_be.resources.catalog.appointment import AppointmentCatalogBuilder

HEALTH_CONTRACT_SCORE = 20
HEALTH_DISCOVER_SCORE = 10


class AppointmentFlow(BecknFlow):
    name = "appointment"
    context_builder = AppointmentContextBuilder
    state_model = AppointmentState
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
        return (attributes.coordination_ref or "") if isinstance(attributes, HealthContract) else ""
