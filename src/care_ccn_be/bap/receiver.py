import logging

from django.db import transaction
from pydantic import ValidationError
from rest_framework.exceptions import ParseError
from rest_framework.response import Response
from rest_framework.views import APIView

from care_ccn_be import store
from care_ccn_be.flows.registry import FlowRegistry
from care_ccn_be.models import Processing, Side
from care_ccn_be.protocol.ack import INVALID_PAYLOAD, UNSUPPORTED_ACTION, ack, nack, nack_from
from care_ccn_be.protocol.actions import ACTIONS, is_callback, request_of
from care_ccn_be.protocol.schema import BecknRequest, InboundMessage
from care_ccn_be.security import BecknReceiverPermission
from care_ccn_be.settings import plugin_settings
from care_ccn_be.tasks import process_bap_callback

logger = logging.getLogger(__name__)

UNKNOWN_TRANSACTION = "70004"


class BapReceiverView(APIView):
    """``/bap/receiver[/<on_action>]``: validate, dedupe, persist, ACK; applying the callback runs in Celery."""

    authentication_classes: list = []
    permission_classes = [BecknReceiverPermission]

    def post(self, request, action: str | None = None):
        try:
            payload = request.data
        except ParseError:
            return Response(nack(INVALID_PAYLOAD, "body is not valid JSON"))
        if not isinstance(payload, dict):
            return Response(nack(INVALID_PAYLOAD, "body must be a JSON object"))
        context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        action = (action or context.get("action") or "").lower().strip("/")
        if not is_callback(action) or request_of(action) not in ACTIONS:
            return Response(
                store.record_nack(payload, action, nack(UNSUPPORTED_ACTION, f"unsupported action {action!r}"), Side.BAP)
            )
        try:
            parsed = BecknRequest[InboundMessage].model_validate(payload)
        except ValidationError as exc:
            return Response(store.record_nack(payload, action, nack_from(exc), Side.BAP))

        txn = store.find_bap_transaction(parsed.context.transaction_id)
        if txn is None or not FlowRegistry.is_enabled(txn.flow):
            body = nack(UNKNOWN_TRANSACTION, "unknown transaction")
            return Response(store.record_nack(payload, action, body, Side.BAP))

        sender = store.inbound_sender(context, Side.BAP, payload)
        existing = store.find_inbound(context, action, sender)
        if existing is not None:
            return Response(existing.ack or ack())
        with transaction.atomic():
            msg = store.record_inbound(txn, payload, action, ack(), sender)
            queue = plugin_settings.BECKN_CELERY_QUEUE or None
            transaction.on_commit(lambda: process_bap_callback.apply_async(args=[msg.pk], queue=queue), robust=True)
        return Response(msg.ack or ack())


def process_callback(message_pk: int) -> str:
    with transaction.atomic():
        msg = store.claim_inbound(message_pk)
        if msg is None or msg.transaction is None:
            return "skipped"
        txn = store.lock(msg.transaction)
        msg.transaction = txn
        flow = FlowRegistry.get(txn.flow)
        store.apply_callback(msg)
        try:
            with transaction.atomic():
                flow.callbacks().handle(txn, msg)
        except Exception:
            logger.exception("flow hook for %s failed", msg.action)
            store.finish_inbound(msg, Processing.FAILED)
            return "hook_failed"
        store.finish_inbound(msg)
    return "processed"
