import json
from typing import Any, ClassVar

from django.db import transaction
from pydantic import ValidationError
from rest_framework import viewsets
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from care_ccn_be import store
from care_ccn_be.bap import read
from care_ccn_be.bap.pipeline import dispatch_beckn_action
from care_ccn_be.bap.specs import BapSpec, DiscoverSpec
from care_ccn_be.exceptions import BecknError
from care_ccn_be.models import BecknTransaction, Side
from care_ccn_be.protocol.actions import DISCOVER


def beckn_exception_handler(exc, context):
    if isinstance(exc, ValidationError):
        return Response({"errors": json.loads(exc.json(include_url=False, include_context=False))}, status=400)
    if isinstance(exc, BecknError):
        return Response({"detail": exc.message, "code": exc.code}, status=400)
    return drf_exception_handler(exc, context)


class BapViewSetBase(viewsets.ViewSet):
    """One subclass per flow, composed from the per-action mixins in ``bap.mixins``."""

    flow: ClassVar[type]
    permission_classes = [IsAuthenticated]
    lookup_field = "transaction_id"
    lookup_value_regex = "[^/]+"
    spec_models: ClassVar[dict[str, type[BapSpec]]] = {DISCOVER: DiscoverSpec}

    def get_exception_handler(self):
        return beckn_exception_handler

    def get_txn(self, transaction_id: str) -> BecknTransaction:
        txn = BecknTransaction.objects.filter(transaction_id=transaction_id, side=Side.BAP, flow=self.flow.name).first()
        if txn is None:
            raise NotFound("Unknown transaction")
        return txn

    def validate_passthrough(self, action: str, message: dict[str, Any]) -> dict[str, Any]:
        """Hook: validate a passthrough ``message`` against the flow's schema; the default accepts it as is."""
        return message

    def after_send(self, action: str, source, msg) -> None:
        """Hook: flow side effects once the request is persisted (still inside the transaction)."""

    def run(self, action: str, request, transaction_id: str | None = None) -> Response:
        data = request.data if isinstance(request.data, dict) else {}
        spec = self.spec_models.get(action, BapSpec).model_validate(data)
        with transaction.atomic():
            txn = (
                store.open_bap_transaction(self.flow, request.user)
                if action == DISCOVER
                else self.get_txn(transaction_id)
            )
            msg = dispatch_beckn_action(self, action, txn, spec)
        body = {"transactionId": txn.transaction_id, "messageId": msg.message_id, "action": action, "status": "QUEUED"}
        return Response(body, status=202)

    def retrieve(self, request, transaction_id: str | None = None):
        txn = self.get_txn(transaction_id)
        action = request.query_params.get("action")
        return Response(read.action_view(txn, action) if action else read.poll(self.flow, txn))
