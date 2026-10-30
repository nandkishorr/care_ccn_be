"""A minimal test-only flow that exercises every framework path without touching Care models."""

from care_ccn_be.bap.callbacks import BapCallbacksBase
from care_ccn_be.bap.mixins import (
    BapCancelMixin,
    BapConfirmMixin,
    BapDiscoverMixin,
    BapInitMixin,
    BapSelectMixin,
    BapStatusMixin,
)
from care_ccn_be.bap.viewset import BapViewSetBase
from care_ccn_be.bpp.mixins import (
    BppCancelMixin,
    BppConfirmMixin,
    BppDiscoverMixin,
    BppInitMixin,
    BppSelectMixin,
)
from care_ccn_be.bpp.service import BppServiceBase
from care_ccn_be.exceptions import BecknError
from care_ccn_be.flows.base import BecknFlow
from care_ccn_be.flows.registry import FlowRegistry
from care_ccn_be.protocol.builder import CatalogBuilder

NETWORK = "echo-net"
HOOKS: list[tuple[str, str]] = []


class EchoCatalog(CatalogBuilder):
    def build(self, spec):
        return {"id": "echo-catalog", "resources": []}


class EchoBppService(BppDiscoverMixin, BppSelectMixin, BppInitMixin, BppConfirmMixin, BppCancelMixin, BppServiceBase):
    def bpp_init(self, call):
        contract_id = (call.message.get("contract") or {}).get("id")
        if contract_id == "boom":
            raise BecknError("ECHO-1", "init refused")
        if contract_id == "crash":
            raise RuntimeError("unexpected")
        return super().bpp_init(call)

    def bpp_confirm(self, call):
        message = super().bpp_confirm(call)
        message["contract"]["id"] = "echo-contract-1"
        return message


class EchoCallbacks(BapCallbacksBase):
    def on_confirm(self, call):
        HOOKS.append((call.txn.transaction_id, call.msg.action))

    def on_cancel(self, call):
        raise RuntimeError("hook exploded")


@FlowRegistry.register
class EchoFlow(BecknFlow):
    name = "echo"
    aliases = ("echoing",)
    bap_viewset = "care_ccn_be.tests.framework.echo_flow.EchoBapViewSet"
    bap_callbacks = EchoCallbacks
    bpp_service = EchoBppService
    catalog_builder = EchoCatalog

    @classmethod
    def match(cls, request):
        return 5 if request.context.network_id == NETWORK else 0

    @classmethod
    def coordination_id(cls, request):
        return "coord-1"


class EchoBapViewSet(
    BapDiscoverMixin,
    BapSelectMixin,
    BapInitMixin,
    BapConfirmMixin,
    BapStatusMixin,
    BapCancelMixin,
    BapViewSetBase,
):
    flow = EchoFlow

    def bap_select(self, source):
        return {"contract": {"status": {"code": "DRAFT"}}}

    def bap_init(self, source):
        return {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}}

    def bap_confirm(self, source):
        return {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}}

    def bap_status(self, source):
        return {"contract": {"id": source.txn.contract_id}}

    def bap_cancel(self, source):
        return {"contract": {"id": source.txn.contract_id}}
