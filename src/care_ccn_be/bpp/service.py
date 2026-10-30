"""BPP side: one service class per flow, composed from per-action mixins."""

from dataclasses import dataclass, field
from typing import Any, ClassVar

from django.db import transaction

from care_ccn_be import store
from care_ccn_be.exceptions import BecknError
from care_ccn_be.models import BecknMessage, BecknTransaction, Processing
from care_ccn_be.protocol.actions import ACTIONS
from care_ccn_be.protocol.builder import ContractBuilder
from care_ccn_be.protocol.schema import BecknRequest


@dataclass
class BppCall:
    """Everything an action handler needs; ``message`` is the raw inbound ``message`` dict."""

    flow: type
    txn: BecknTransaction
    inbound: BecknMessage
    request: BecknRequest
    message: dict[str, Any] = field(default_factory=dict)

    @property
    def context(self) -> dict[str, Any]:
        return self.inbound.payload.get("context") or {}


class BppServiceBase:
    """Handlers are named ``bpp_<action>`` and return the callback ``message`` (dict or BecknModel)."""

    contract_builder: ClassVar[type[ContractBuilder]] = ContractBuilder

    def __init__(self, flow):
        self.flow = flow

    @classmethod
    def supported_actions(cls) -> frozenset[str]:
        return frozenset(action for action in ACTIONS if callable(getattr(cls, f"bpp_{action}", None)))

    def handle(self, call: BppCall):
        handler = getattr(self, f"bpp_{call.request.context.action}", None)
        if handler is None:
            raise BecknError("70001", f"action {call.request.context.action!r} is not supported by {self.flow.name}")
        return handler(call)


def process_inbound(message_pk: int) -> str:
    with transaction.atomic():
        msg = store.claim_inbound(message_pk)
        if msg is None or msg.transaction is None:
            return "skipped"
        txn = store.lock(msg.transaction)
        from care_ccn_be.flows.registry import FlowRegistry

        flow = FlowRegistry.get(txn.flow)
        request = BecknRequest.model_validate(msg.payload)
        call = BppCall(flow, txn, msg, request, dict(msg.payload.get("message") or {}))
        error = None
        try:
            with transaction.atomic():
                reply = flow.service().handle(call)
        except BecknError as exc:
            reply, error = call.message, exc.error_block()
        context = flow.context().callback(call.context, msg.action)
        out = store.record_outbound(txn, context, reply, error, reply_to=msg)
        store.finish_inbound(msg, Processing.PROCESSED if error is None else Processing.FAILED)
        store.enqueue_delivery(out)
    return "failed" if error else "processed"
