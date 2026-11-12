"""A minimal test-only flow that exercises every framework path without touching Care models."""

from care_ccn_be.api.viewsets.base import (
    BecknViewSet,
    CancelMixin,
    ConfirmMixin,
    DiscoverMixin,
    InitMixin,
    SelectMixin,
    StatusMixin,
)
from care_ccn_be.api.viewsets.lookup import VIEWSETS
from care_ccn_be.exceptions import BecknError
from care_ccn_be.flows.base import BecknFlow
from care_ccn_be.flows.lookup import FLOWS
from care_ccn_be.protocol.builder import CatalogBuilder

NETWORK = "echo-net"
HOOKS: list[tuple[str, str]] = []


class EchoCatalog(CatalogBuilder):
    def build(self, spec):
        return {"id": "echo-catalog", "resources": []}


class EchoFlow(BecknFlow):
    name = "echo"
    aliases = ("echoing",)
    catalog_builder = EchoCatalog

    @classmethod
    def match(cls, request):
        return 5 if request.context.network_id == NETWORK else 0

    @classmethod
    def coordination_id(cls, request):
        return "coord-1"


class EchoViewSet(DiscoverMixin, SelectMixin, InitMixin, ConfirmMixin, StatusMixin, CancelMixin, BecknViewSet):
    flow = EchoFlow
    on_status_spec = None

    def build_select(self, request_obj, txn, state):
        return {"contract": {"status": {"code": "DRAFT"}}}

    def build_init(self, request_obj, txn, state):
        return {"contract": {"id": "c-1", "status": {"code": "DRAFT"}}}

    def build_confirm(self, request_obj, txn, state):
        return {"contract": {"id": "c-1", "status": {"code": "ACTIVE"}}}

    def build_status(self, request_obj, txn, state):
        return {"contract": {"id": txn.contract_id}}

    def build_cancel(self, request_obj, txn, state):
        return {"contract": {"id": txn.contract_id}}

    def on_confirm(self, call):
        HOOKS.append((call.txn.transaction_id, call.msg.action))

    def on_cancel(self, call):
        raise RuntimeError("hook exploded")

    def perform_on_init(self, request_obj, call):
        contract_id = (call.message.get("contract") or {}).get("id")
        if contract_id == "boom":
            raise BecknError("ECHO-1", "init refused")
        if contract_id == "crash":
            raise RuntimeError("unexpected")
        return super().perform_on_init(request_obj, call)

    def perform_on_confirm(self, request_obj, call):
        message = super().perform_on_confirm(request_obj, call)
        message["contract"]["id"] = "echo-contract-1"
        return message


FLOWS[EchoFlow.name] = EchoFlow
VIEWSETS[EchoFlow.name] = EchoViewSet
