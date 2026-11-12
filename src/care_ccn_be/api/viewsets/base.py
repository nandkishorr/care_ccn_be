"""Shared Beckn viewset foundation, including the BAP and BPP action mixins.

Permissions are not implemented yet; they plug into the EMR hooks: ``get_queryset`` (which transactions a user
sees), ``authorize_retrieve``, BAP ``authorize_<action>`` hooks and BPP ``authorize_on_<action>`` hooks.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar

from care.emr.api.viewsets.base import EMRBaseViewSet, EMRListMixin, EMRRetrieveMixin, emr_exception_handler
from care.utils.swagger.schema import AutoSchema
from django.db import transaction
from django_filters import rest_framework as filters
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from care_ccn_be import store
from care_ccn_be.exceptions import BecknError
from care_ccn_be.models import Adapter, BecknMessage, BecknTransaction, Processing
from care_ccn_be.protocol.ack import INVALID_PAYLOAD, UNSUPPORTED_ACTION
from care_ccn_be.protocol.actions import (
    ACTIONS,
    CANCEL,
    CONFIRM,
    CONTRACT_ACTIVE,
    CONTRACT_CANCELLED,
    CONTRACT_DRAFT,
    DISCOVER,
    INIT,
    SELECT,
    STATUS,
    UPDATE,
)
from care_ccn_be.protocol.builder import ContractBuilder
from care_ccn_be.protocol.discover import build_discover_intent
from care_ccn_be.resources.bap.spec import BapActionResponseSpec, BapActionSpec, BapCatalogsSpec, BapDiscoverSpec
from care_ccn_be.resources.beckn import BecknModel, BecknRequest, DiscoverMessage, InboundMessage
from care_ccn_be.resources.transaction.spec import BecknTransactionListSpec, BecknTransactionRetrieveSpec


def beckn_exception_handler(exc, context):
    if isinstance(exc, BecknError):
        return Response({"errors": [{"type": "beckn_error", "code": exc.code, "msg": exc.message}]}, status=400)
    return emr_exception_handler(exc, context)


class BecknTransactionFilters(filters.FilterSet):
    transaction_id = filters.CharFilter(field_name="transaction_id")
    status = filters.CharFilter(field_name="status", lookup_expr="iexact")
    contract_id = filters.CharFilter(field_name="contract_id")
    coordination_id = filters.CharFilter(field_name="coordination_id")


class BecknAutoSchema(AutoSchema):
    """CARE's schema plus the action APIs, documented from ``<action>_spec``."""

    def action_spec(self):
        action = getattr(self.view, "action", None)
        if action == "update_contract":
            action = "update"
        return getattr(self.view, f"{action}_spec", None) if action else None

    def get_request_serializer(self):
        return self.action_spec() or super().get_request_serializer()

    def get_response_serializers(self):
        if self.action_spec() is not None:
            return {202: BapActionResponseSpec}
        if getattr(self.view, "action", None) == "catalogs":
            return {200: BapCatalogsSpec}
        return super().get_response_serializers()


class BecknBaseViewSet(EMRBaseViewSet):
    """The flow's ``viewset`` path points here; ``adapters`` limits which adapters it serves."""

    adapter = None
    adapters: ClassVar[tuple[str, ...]] = ()
    database_model = BecknTransaction
    pydantic_read_model = BecknTransactionListSpec
    pydantic_retrieve_model = BecknTransactionRetrieveSpec
    lookup_field = "transaction_id"
    lookup_value_regex = "[^/]+"
    filter_backends = [filters.DjangoFilterBackend]
    filterset_class = BecknTransactionFilters
    schema = BecknAutoSchema()

    def get_exception_handler(self):
        return beckn_exception_handler

    def get_queryset(self):
        """Permission hook (later): narrow the transactions a user may see."""
        return (
            BecknTransaction.objects.filter(adapter=self.adapter, flow=self.flow.name)
            .select_related("patient", "facility")
            .order_by("-modified_date")
        )


logger = logging.getLogger(__name__)

NOT_IMPLEMENTED = "70003"

SpecT = TypeVar("SpecT", bound=BaseModel)


def not_implemented(action_name: str):
    raise BecknError(NOT_IMPLEMENTED, f"{action_name} is not implemented for this flow")


class BapViewSet(EMRRetrieveMixin, EMRListMixin, BecknBaseViewSet):
    """Every BAP endpoint goes through ``run_action``: the body is validated against ``<action>_spec``, then,
    in one DB transaction, ``authorize_<action>`` -> ``build_<action>`` (or the passthrough ``message``) -> stored
    request -> ``perform_<action>``. The defaults of those hooks live on the action mixins.
    """

    adapter = Adapter.BAP.value

    def get_request_obj(self, spec: type[SpecT], request) -> SpecT:
        if not isinstance(request.data, dict):
            raise ValidationError("The request body must be a JSON object")
        try:
            return spec.model_validate(request.data)
        except PydanticValidationError as exc:
            reasons = "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors()[:5])
            raise ValidationError(reasons) from exc

    def run_action(self, action_name: str, request) -> Response:
        request_obj = self.get_request_obj(getattr(self, f"{action_name}_spec"), request)
        txn = None if action_name == DISCOVER else self.get_object()
        with transaction.atomic():
            txn = store.open_bap_transaction(self.flow, self.request.user) if txn is None else store.lock(txn)
            store.remember_overrides(txn, self.flow, action_name, request_obj.context_overrides())
            self.authorize_action(action_name, request_obj, txn)
            msg = self.record_request(action_name, request_obj, txn)
            self.perform_action(action_name, request_obj, txn, msg)
            store.enqueue_delivery(msg)
        body = BapActionResponseSpec(
            transaction_id=txn.transaction_id, message_id=msg.message_id, action=action_name, status=txn.status
        )
        return Response(body.model_dump(mode="json"), status=202)

    def authorize_action(self, action_name: str, request_obj: BapActionSpec, txn: BecknTransaction) -> None:
        """Runs before the message is built; raising aborts the whole action."""
        getattr(self, f"authorize_{action_name}")(request_obj, txn)

    def perform_action(
        self, action_name: str, request_obj: BapActionSpec, txn: BecknTransaction, msg: BecknMessage
    ) -> None:
        getattr(self, f"perform_{action_name}")(request_obj, txn, msg)

    def validate_passthrough(self, action_name: str, message: Any) -> Any:
        return message

    def record_request(self, action_name: str, request_obj: BapActionSpec, txn: BecknTransaction) -> BecknMessage:
        """Store the outbound request (status ``QUEUED``); delivery is enqueued by ``run_action``."""
        state = store.get_state(txn, self.flow)
        if request_obj.message is not None:
            message = self.validate_passthrough(action_name, request_obj.message)
            if isinstance(message, BecknModel):
                message = message.dump()
        else:
            message = getattr(self, f"build_{action_name}")(request_obj, txn, state)
        routing = {} if action_name == DISCOVER else {"bppId": txn.counterparty_id, "bppUri": txn.counterparty_uri}
        context = self.flow.context().request(action_name, txn.transaction_id, routing, state.context_overrides)
        return store.record_outbound(txn, context, message)


# ----------------------------------------------------------------------------- callbacks (Celery)
@dataclass
class CallbackCall:
    """What an ``on_<action>`` hook receives."""

    flow: type
    txn: BecknTransaction
    msg: BecknMessage

    @property
    def message(self) -> dict:
        return self.msg.payload.get("message") or {}

    @property
    def error(self) -> dict | None:
        return self.msg.payload.get("error")


def process_callback(message_pk: int) -> str:
    """Apply one stored ``on_*`` callback to its BAP transaction, then run the flow viewset's ``on_<action>``."""
    from care_ccn_be.api.viewsets.lookup import get_viewset  # imports the flow viewsets, which import this module
    from care_ccn_be.flows.lookup import get_flow

    with transaction.atomic():
        msg = store.claim_inbound(message_pk)
        if msg is None or msg.transaction is None:
            return "skipped"
        txn = store.lock(msg.transaction)
        msg.transaction = txn
        flow = get_flow(txn.flow)
        store.apply_callback(msg)
        viewset = get_viewset(Adapter.BAP.value, flow.name)
        hook = getattr(viewset(adapter=Adapter.BAP.value), msg.action, None) if viewset else None
        try:
            with transaction.atomic():
                if callable(hook):
                    hook(CallbackCall(flow, txn, msg))
        except Exception:
            logger.exception("flow hook for %s failed", msg.action)
            store.finish_inbound(msg, Processing.FAILED)
            return "hook_failed"
        store.finish_inbound(msg)
    return "processed"


@dataclass
class BppCall:
    """Everything a ``perform_on_<action>`` needs; ``message`` is the raw inbound ``message`` dict."""

    flow: type
    txn: BecknTransaction
    inbound: BecknMessage
    request: BecknRequest
    message: dict[str, Any] = field(default_factory=dict)

    @property
    def context(self) -> dict[str, Any]:
        return self.inbound.payload.get("context") or {}


class BppViewSet(EMRRetrieveMixin, EMRListMixin, BecknBaseViewSet):
    """Inbound requests: ``validate_request`` (``on_<action>_spec``) and ``authorize_request`` run before the ACK,
    ``perform_request`` runs in Celery. Flows override ``authorize_on_<action>`` / ``perform_on_<action>``.
    """

    adapter = Adapter.BPP.value
    contract_builder: ClassVar[type[ContractBuilder]] = ContractBuilder

    @classmethod
    def supported_actions(cls) -> frozenset[str]:
        return frozenset(name for name in ACTIONS if getattr(cls, f"on_{name}_spec", None) is not None)

    def validate_request(self, action_name: str, message: dict[str, Any] | None) -> BaseModel:
        """Validate the inbound ``message`` against ``on_<action>_spec`` (``BecknError 70002`` when invalid)."""
        if action_name not in self.supported_actions():
            raise BecknError(UNSUPPORTED_ACTION, f"action {action_name!r} is not supported by {self.flow.name}")
        spec: type[BaseModel] = getattr(self, f"on_{action_name}_spec")
        try:
            return spec.model_validate(message or {})
        except PydanticValidationError as exc:
            reasons = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            raise BecknError(INVALID_PAYLOAD, reasons) from exc

    def authorize_request(self, action_name: str, request_obj: BaseModel, beckn_request: BecknRequest) -> None:
        getattr(self, f"authorize_on_{action_name}")(request_obj, beckn_request)

    def perform_request(self, action_name: str, request_obj: BaseModel, call: BppCall) -> dict[str, Any]:
        return getattr(self, f"perform_on_{action_name}")(request_obj, call)


class BecknViewSet(BapViewSet, BppViewSet):
    """One flow viewset with BAP request/callback hooks and BPP request handlers.

    HTTP routes and background processing bind ``adapter`` per instance; it must never be shared class state.
    """

    adapter = None
    adapters = (Adapter.BAP.value, Adapter.BPP.value)


# ----------------------------------------------------------------------------- action mixins
# One mixin per Beckn action, both sides: BAP ``<action>`` endpoint + ``on_<action>`` callback, and the BPP
# ``on_<action>_spec`` + ``perform_on_<action>``. Set ``on_<action>_spec = None`` to keep an action BAP-only.
class DiscoverMixin:
    discover_spec: ClassVar[type[BapActionSpec]] = BapDiscoverSpec
    on_discover_spec: ClassVar[type[BaseModel] | None] = DiscoverMessage
    discover_filter_keys: ClassVar[tuple[str, ...]] = ("healthServiceType", "acceptanceMode")

    def authorize_discover(self, request_obj: BapDiscoverSpec, txn: BecknTransaction) -> None:
        pass

    def build_discover(self, request_obj: BapDiscoverSpec, txn: BecknTransaction, state) -> dict[str, Any]:
        return {"intent": build_discover_intent(request_obj.intent_query(), self.discover_filter_keys)}

    def perform_discover(self, request_obj: BapDiscoverSpec, txn: BecknTransaction, msg: BecknMessage) -> None:
        pass

    def on_discover(self, call: CallbackCall) -> None:
        pass

    def authorize_on_discover(self, request_obj: BaseModel, beckn_request: BecknRequest) -> None:
        pass

    def perform_on_discover(self, request_obj: BaseModel, call: BppCall) -> dict[str, Any]:
        if self.flow.catalog_builder is None:
            raise BecknError(NOT_IMPLEMENTED, f"{self.flow.name} publishes no catalog")
        return {"catalogs": [self.flow.catalog_builder().build(call)]}

    @action(detail=False, methods=["POST"])
    def discover(self, request, *args, **kwargs):
        return self.run_action(DISCOVER, request)

    @action(detail=True, methods=["GET"])
    def catalogs(self, request, *args, **kwargs):
        """The catalogs of every ``on_discover`` responder, merged."""
        txn = self.get_object()
        self.authorize_retrieve(txn)
        body = BapCatalogsSpec(transaction_id=txn.transaction_id, catalogs=store.merged_catalogs(txn))
        return Response(body.model_dump(mode="json"))


class SelectMixin:
    select_spec: ClassVar[type[BapActionSpec]] = BapActionSpec
    on_select_spec: ClassVar[type[BaseModel] | None] = InboundMessage

    def authorize_select(self, request_obj: BapActionSpec, txn: BecknTransaction) -> None:
        pass

    def build_select(self, request_obj: BapActionSpec, txn: BecknTransaction, state) -> dict[str, Any]:
        not_implemented(SELECT)

    def perform_select(self, request_obj: BapActionSpec, txn: BecknTransaction, msg: BecknMessage) -> None:
        pass

    def on_select(self, call: CallbackCall) -> None:
        pass

    def authorize_on_select(self, request_obj: BaseModel, beckn_request: BecknRequest) -> None:
        pass

    def perform_on_select(self, request_obj: BaseModel, call: BppCall) -> dict[str, Any]:
        return self.contract_builder.echo(call.message, CONTRACT_DRAFT)

    @action(detail=True, methods=["POST"])
    def select(self, request, *args, **kwargs):
        return self.run_action(SELECT, request)


class InitMixin:
    init_spec: ClassVar[type[BapActionSpec]] = BapActionSpec
    on_init_spec: ClassVar[type[BaseModel] | None] = InboundMessage

    def authorize_init(self, request_obj: BapActionSpec, txn: BecknTransaction) -> None:
        pass

    def build_init(self, request_obj: BapActionSpec, txn: BecknTransaction, state) -> dict[str, Any]:
        not_implemented(INIT)

    def perform_init(self, request_obj: BapActionSpec, txn: BecknTransaction, msg: BecknMessage) -> None:
        pass

    def on_init(self, call: CallbackCall) -> None:
        pass

    def authorize_on_init(self, request_obj: BaseModel, beckn_request: BecknRequest) -> None:
        pass

    def perform_on_init(self, request_obj: BaseModel, call: BppCall) -> dict[str, Any]:
        return self.contract_builder.echo(call.message, CONTRACT_DRAFT)

    @action(detail=True, methods=["POST"])
    def init(self, request, *args, **kwargs):
        return self.run_action(INIT, request)


class ConfirmMixin:
    confirm_spec: ClassVar[type[BapActionSpec]] = BapActionSpec
    on_confirm_spec: ClassVar[type[BaseModel] | None] = InboundMessage

    def authorize_confirm(self, request_obj: BapActionSpec, txn: BecknTransaction) -> None:
        pass

    def build_confirm(self, request_obj: BapActionSpec, txn: BecknTransaction, state) -> dict[str, Any]:
        not_implemented(CONFIRM)

    def perform_confirm(self, request_obj: BapActionSpec, txn: BecknTransaction, msg: BecknMessage) -> None:
        pass

    def on_confirm(self, call: CallbackCall) -> None:
        pass

    def authorize_on_confirm(self, request_obj: BaseModel, beckn_request: BecknRequest) -> None:
        pass

    def perform_on_confirm(self, request_obj: BaseModel, call: BppCall) -> dict[str, Any]:
        return self.contract_builder.echo(call.message, CONTRACT_ACTIVE)

    @action(detail=True, methods=["POST"])
    def confirm(self, request, *args, **kwargs):
        return self.run_action(CONFIRM, request)


class StatusMixin:
    status_spec: ClassVar[type[BapActionSpec]] = BapActionSpec
    on_status_spec: ClassVar[type[BaseModel] | None] = InboundMessage

    def authorize_status(self, request_obj: BapActionSpec, txn: BecknTransaction) -> None:
        pass

    def build_status(self, request_obj: BapActionSpec, txn: BecknTransaction, state) -> dict[str, Any]:
        not_implemented(STATUS)

    def perform_status(self, request_obj: BapActionSpec, txn: BecknTransaction, msg: BecknMessage) -> None:
        pass

    def on_status(self, call: CallbackCall) -> None:
        pass

    def authorize_on_status(self, request_obj: BaseModel, beckn_request: BecknRequest) -> None:
        pass

    def perform_on_status(self, request_obj: BaseModel, call: BppCall) -> dict[str, Any]:
        raise BecknError(NOT_IMPLEMENTED, f"{self.flow.name} does not implement status")

    @action(detail=True, methods=["POST"])
    def status(self, request, *args, **kwargs):
        return self.run_action(STATUS, request)


class UpdateMixin:
    update_spec: ClassVar[type[BapActionSpec]] = BapActionSpec
    on_update_spec: ClassVar[type[BaseModel] | None] = InboundMessage

    def authorize_update(self, request_obj: BapActionSpec, txn: BecknTransaction) -> None:
        pass

    def build_update(self, request_obj: BapActionSpec, txn: BecknTransaction, state) -> dict[str, Any]:
        not_implemented(UPDATE)

    def perform_update(self, request_obj: BapActionSpec, txn: BecknTransaction, msg: BecknMessage) -> None:
        pass

    def on_update(self, call: CallbackCall) -> None:
        pass

    def authorize_on_update(self, request_obj: BaseModel, beckn_request: BecknRequest) -> None:
        pass

    def perform_on_update(self, request_obj: BaseModel, call: BppCall) -> dict[str, Any]:
        raise BecknError(NOT_IMPLEMENTED, f"{self.flow.name} does not implement update")

    # not named ``update``: DRF reserves that name for the router's PUT route
    @action(detail=True, methods=["POST"], url_path="update", url_name="update")
    def update_contract(self, request, *args, **kwargs):
        return self.run_action(UPDATE, request)


class CancelMixin:
    cancel_spec: ClassVar[type[BapActionSpec]] = BapActionSpec
    on_cancel_spec: ClassVar[type[BaseModel] | None] = InboundMessage

    def authorize_cancel(self, request_obj: BapActionSpec, txn: BecknTransaction) -> None:
        pass

    def build_cancel(self, request_obj: BapActionSpec, txn: BecknTransaction, state) -> dict[str, Any]:
        not_implemented(CANCEL)

    def perform_cancel(self, request_obj: BapActionSpec, txn: BecknTransaction, msg: BecknMessage) -> None:
        pass

    def on_cancel(self, call: CallbackCall) -> None:
        pass

    def authorize_on_cancel(self, request_obj: BaseModel, beckn_request: BecknRequest) -> None:
        pass

    def perform_on_cancel(self, request_obj: BaseModel, call: BppCall) -> dict[str, Any]:
        return self.contract_builder.echo(call.message, CONTRACT_CANCELLED)

    @action(detail=True, methods=["POST"])
    def cancel(self, request, *args, **kwargs):
        return self.run_action(CANCEL, request)


# ----------------------------------------------------------------------------- processing (Celery)
def process_inbound(message_pk: int) -> str:
    """Run ``perform_on_<action>`` of the flow's BPP viewset for one stored request and queue its reply."""
    from care_ccn_be.api.viewsets.lookup import get_viewset
    from care_ccn_be.flows.lookup import get_flow

    with transaction.atomic():
        msg = store.claim_inbound(message_pk)
        if msg is None or msg.transaction is None:
            return "skipped"
        txn = store.lock(msg.transaction)
        flow = get_flow(txn.flow)
        request = BecknRequest.model_validate(msg.payload)
        call = BppCall(flow, txn, msg, request, dict(msg.payload.get("message") or {}))
        error = None
        try:
            viewset = get_viewset(Adapter.BPP.value, flow.name)
            if viewset is None:
                raise BecknError(UNSUPPORTED_ACTION, f"{flow.name} has no BPP")
            handler = viewset(adapter=Adapter.BPP.value)
            request_obj = handler.validate_request(msg.action, call.message)
            with transaction.atomic():
                reply = handler.perform_request(msg.action, request_obj, call)
        except BecknError as exc:
            reply, error = call.message, exc.error_block()
        context = flow.context().callback(call.context, msg.action)
        out = store.record_outbound(txn, context, reply, error, reply_to=msg)
        store.finish_inbound(msg, Processing.PROCESSED if error is None else Processing.FAILED)
        store.enqueue_delivery(out)
    return "failed" if error else "processed"
