from django.db import transaction
from pydantic import ValidationError
from rest_framework.exceptions import ParseError
from rest_framework.response import Response
from rest_framework.views import APIView

from care_ccn_be import store
from care_ccn_be.bpp.routing import resolve_flow
from care_ccn_be.models import Side
from care_ccn_be.protocol.ack import INVALID_PAYLOAD, UNSUPPORTED_ACTION, ack, nack, nack_from
from care_ccn_be.protocol.actions import ACTIONS, is_callback
from care_ccn_be.protocol.schema import BecknRequest, InboundMessage
from care_ccn_be.security import BecknReceiverPermission
from care_ccn_be.settings import plugin_settings
from care_ccn_be.tasks import process_bpp_request


class BppReceiverView(APIView):
    """``/bpp/receiver[/<action>]``: validate, dedupe, persist, ACK; the real work runs in Celery."""

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
        if action not in ACTIONS or is_callback(action):
            return Response(
                store.record_nack(payload, action, nack(UNSUPPORTED_ACTION, f"unsupported action {action!r}"), Side.BPP)
            )
        try:
            parsed = BecknRequest[InboundMessage].model_validate(payload)
        except ValidationError as exc:
            return Response(store.record_nack(payload, action, nack_from(exc), Side.BPP))

        sender = store.inbound_sender(context, Side.BPP)
        existing = store.find_inbound(context, action, sender)
        if existing is not None:
            return Response(existing.ack or ack())

        flow = resolve_flow(parsed)
        if flow is None or flow.bpp_service is None or action not in flow.bpp_service.supported_actions():
            body = nack(UNSUPPORTED_ACTION, f"no flow handles {action!r}")
            return Response(store.record_nack(payload, action, body, Side.BPP))

        with transaction.atomic():
            txn = store.open_bpp_transaction(flow, parsed)
            msg = store.record_inbound(txn, payload, action, ack(), sender)
            queue = plugin_settings.BECKN_CELERY_QUEUE or None
            transaction.on_commit(lambda: process_bpp_request.apply_async(args=[msg.pk], queue=queue), robust=True)
        return Response(msg.ack or ack())
