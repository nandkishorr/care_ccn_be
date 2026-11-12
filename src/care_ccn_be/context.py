"""Flow context builders; protocol-level construction stays in ``protocol.context``."""

from care_ccn_be.constants import HEALTH_RESOURCE_CONTEXT
from care_ccn_be.flows.defaults import cc_endpoint
from care_ccn_be.protocol.context import ContextBuilder, Endpoint
from care_ccn_be.resources.referral.schema import DISCOVER_SCHEMA_CONTEXT


class AppointmentContextBuilder(ContextBuilder):
    discover_schema_context = HEALTH_RESOURCE_CONTEXT.value


class ReferralContext(ContextBuilder):
    discover_schema_context = DISCOVER_SCHEMA_CONTEXT

    def default_bpp(self) -> Endpoint:
        return cc_endpoint()
