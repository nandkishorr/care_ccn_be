"""ONIX entry points, on CARE's EMR base viewset. URLs and the ACK / NACK wire contract are unchanged.

ONIX appends the action to one configured URL per adapter (``bap/receiver/on_<action>``, ``bpp/receiver/<action>``),
so these two views stay per action and hand each message to the matching flow viewset's hooks.
"""

from typing import Any, ClassVar

from care.emr.api.viewsets.base import EMRBaseViewSet
from django.db import transaction
from pydantic import ValidationError
from rest_framework.exceptions import ParseError
from rest_framework.response import Response

from care_ccn_be import store
from care_ccn_be.api.viewsets.lookup import get_viewset
from care_ccn_be.exceptions import BecknError
from care_ccn_be.flows.lookup import get_flow, is_flow_enabled, match_flow
from care_ccn_be.models import Adapter, BecknTransaction
from care_ccn_be.protocol.ack import INVALID_PAYLOAD, UNSUPPORTED_ACTION, ack, nack, nack_from
from care_ccn_be.protocol.actions import ACTIONS, is_callback, request_of
from care_ccn_be.resources.beckn import BecknRequest, InboundMessage
from care_ccn_be.security import BecknReceiverPermission
from care_ccn_be.settings import plugin_settings

UNKNOWN_TRANSACTION = "70004"


def celery_queue() -> str | None:
    return plugin_settings.BECKN_CELERY_QUEUE or None


class BecknReceiverViewSet(EMRBaseViewSet):
    """Parse, validate the envelope, then ``handle``: dedupe, persist, ACK. The real work runs in Celery."""

    adapter: ClassVar[str]
    authentication_classes: list = []
    permission_classes = [BecknReceiverPermission]
    schema = None

    def accepts(self, action: str) -> bool:
        raise NotImplementedError

    def handle(self, payload: dict[str, Any], context: dict[str, Any], action: str, parsed: BecknRequest):
        raise NotImplementedError

    def nack(self, payload: dict[str, Any], action: str, code: str, message: str) -> Response:
        return Response(store.record_nack(payload, action, nack(code, message), self.adapter))

    def receive(self, request, action: str | None = None):
        try:
            payload = request.data
        except ParseError:
            return Response(nack(INVALID_PAYLOAD, "body is not valid JSON"))
        if not isinstance(payload, dict):
            return Response(nack(INVALID_PAYLOAD, "body must be a JSON object"))
        context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        action = (action or context.get("action") or "").lower().strip("/")
        if not self.accepts(action):
            return self.nack(payload, action, UNSUPPORTED_ACTION, f"unsupported action {action!r}")
        try:
            parsed = BecknRequest[InboundMessage].model_validate(payload)
        except ValidationError as exc:
            return Response(store.record_nack(payload, action, nack_from(exc), self.adapter))
        return self.handle(payload, context, action, parsed)


class BapReceiverViewSet(BecknReceiverViewSet):
    """``bap/receiver[/<on_action>]``: ``on_*`` callbacks of the BAP transactions; the flow hook runs in Celery."""

    adapter = Adapter.BAP.value

    def accepts(self, action: str) -> bool:
        return is_callback(action) and request_of(action) in ACTIONS

    def handle(self, payload, context, action, parsed):
        from care_ccn_be.tasks import process_bap_callback

        txn = store.find_bap_transaction(parsed.context.transaction_id)
        if txn is None or not is_flow_enabled(txn.flow):
            return self.nack(payload, action, UNKNOWN_TRANSACTION, "unknown transaction")
        sender = store.inbound_sender(context, Adapter.BAP, payload)
        existing = store.find_inbound(context, action, sender)
        if existing is not None:
            return Response(existing.ack or ack())
        with transaction.atomic():
            msg = store.record_inbound(txn, payload, action, ack(), sender)
            transaction.on_commit(
                lambda: process_bap_callback.apply_async(args=[msg.pk], queue=celery_queue()), robust=True
            )
        return Response(msg.ack or ack())


class BppReceiverViewSet(BecknReceiverViewSet):
    """``bpp/receiver[/<action>]``: requests to Care's BPP. The flow's spec and ``authorize_on_<action>`` run
    before the ACK; its ``perform_on_<action>`` runs in Celery."""

    adapter = Adapter.BPP.value

    def accepts(self, action: str) -> bool:
        return action in ACTIONS and not is_callback(action)

    def handle(self, payload, context, action, parsed):
        from care_ccn_be.tasks import process_bpp_request

        sender = store.inbound_sender(context, Adapter.BPP)
        existing = store.find_inbound(context, action, sender)
        if existing is not None:
            return Response(existing.ack or ack())

        flow = resolve_flow(parsed)
        viewset = get_viewset(Adapter.BPP.value, flow.name) if flow else None
        if viewset is None or action not in viewset.supported_actions():
            return self.nack(payload, action, UNSUPPORTED_ACTION, f"no flow handles {action!r}")
        try:
            handler = viewset(adapter=Adapter.BPP.value)
            request_obj = handler.validate_request(action, payload.get("message"))
            handler.authorize_request(action, request_obj, parsed)
        except BecknError as exc:
            return self.nack(payload, action, exc.code, exc.message)

        with transaction.atomic():
            txn = store.open_bpp_transaction(flow, parsed)
            msg = store.record_inbound(txn, payload, action, ack(), sender)
            transaction.on_commit(
                lambda: process_bpp_request.apply_async(args=[msg.pk], queue=celery_queue()), robust=True
            )
        return Response(msg.ack or ack())


def resolve_flow(request: BecknRequest):
    """Pick the flow an inbound request belongs to: known transaction, then known contract, then payload match."""
    context = request.context
    txn = BecknTransaction.objects.filter(transaction_id=context.transaction_id, adapter=Adapter.BPP).first()
    if txn is None:
        contract = getattr(request.message, "contract", None)
        contract_id = getattr(contract, "id", None)
        if contract_id:
            txn = BecknTransaction.objects.filter(contract_id=contract_id, adapter=Adapter.BPP).first()
    if txn is not None:
        return get_flow(txn.flow) if is_flow_enabled(txn.flow) else None
    return match_flow(request)
