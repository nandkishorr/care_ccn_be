"""The only module that writes ``BecknTransaction`` and ``BecknMessage``."""

import hashlib
import json
import uuid
from datetime import timedelta
from functools import partial
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from care_ccn_be.models import BecknMessage, BecknTransaction, Delivery, Direction, Processing, Side, TxnStatus
from care_ccn_be.protocol.actions import STATUS, UPDATE, callback_of, is_callback, request_of
from care_ccn_be.protocol.client import ACK, SKIPPED, AckResult
from care_ccn_be.protocol.context import clean_overrides, extract_routing
from care_ccn_be.settings import plugin_settings

UNSOLICITED_CALLBACKS = frozenset({callback_of(STATUS), callback_of(UPDATE)})
STUCK_AFTER = timedelta(minutes=10)


# ----------------------------------------------------------------------------- transactions
def open_bap_transaction(
    flow, user=None, transaction_id: str | None = None, coordination_id: str = ""
) -> BecknTransaction:
    return BecknTransaction.objects.create(
        transaction_id=transaction_id or str(uuid.uuid4()),
        side=Side.BAP,
        flow=flow.name,
        coordination_id=coordination_id,
        created_by=user if getattr(user, "pk", None) else None,
    )


def lock(txn: BecknTransaction) -> BecknTransaction:
    return BecknTransaction.objects.select_for_update().get(pk=txn.pk)


def find_bap_transaction(transaction_id: str) -> BecknTransaction | None:
    return BecknTransaction.objects.filter(transaction_id=transaction_id, side=Side.BAP).first()


def find_bpp_transaction(transaction_id: str) -> BecknTransaction | None:
    return BecknTransaction.objects.filter(transaction_id=transaction_id, side=Side.BPP).first()


def open_bpp_transaction(flow, request) -> BecknTransaction:
    context = request.context
    txn, _ = BecknTransaction.objects.get_or_create(
        transaction_id=context.transaction_id,
        side=Side.BPP,
        defaults={
            "flow": flow.name,
            "network_id": context.network_id or "",
            "counterparty_id": context.bap_id or "",
            "counterparty_uri": context.bap_uri or "",
            "coordination_id": flow.coordination_id(request),
        },
    )
    return txn


def update_transaction(txn: BecknTransaction, **fields: Any) -> BecknTransaction:
    for key, value in fields.items():
        setattr(txn, key, value)
    txn.save(update_fields=[*fields, "modified_date"])
    return txn


# ----------------------------------------------------------------------------- state
def get_state(txn: BecknTransaction, flow):
    return flow.state_model.model_validate(txn.state or {})


def update_state(txn: BecknTransaction, flow, **fields: Any):
    state = flow.state_model.model_validate({**(txn.state or {}), **fields})
    txn.state = state.model_dump(mode="json")
    txn.save(update_fields=["state", "modified_date"])
    return state


def remember_overrides(txn: BecknTransaction, flow, action: str, context: dict | None):
    cleaned = clean_overrides(action, context)
    state = get_state(txn, flow)
    if not cleaned:
        return state
    return update_state(txn, flow, context_overrides={**state.context_overrides, **cleaned})


# ----------------------------------------------------------------------------- messages
def payload_hash(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:32]


def inbound_sender(context: dict, side: str, payload: Any = None) -> str:
    if side == Side.BPP:
        return context.get("bapId") or context.get("bapUri") or ""
    return context.get("bppId") or context.get("bppUri") or payload_hash(payload)


def find_inbound(context: dict, action: str, sender_id: str) -> BecknMessage | None:
    return BecknMessage.objects.filter(
        message_id=context.get("messageId", ""), action=action, direction=Direction.IN, sender_id=sender_id
    ).first()


def record_inbound(
    txn: BecknTransaction | None,
    payload: dict,
    action: str,
    ack: dict,
    sender_id: str = "",
    status: str = Processing.PENDING,
) -> BecknMessage:
    context = payload.get("context") or {}
    try:
        with transaction.atomic():
            msg = BecknMessage.objects.create(
                transaction=txn,
                message_id=context.get("messageId", ""),
                action=action,
                direction=Direction.IN,
                sender_id=sender_id,
                payload=payload,
                ack=ack,
                processing_status=status,
            )
    except IntegrityError:  # a concurrent retry won the race: answer with what is stored
        return find_inbound(context, action, sender_id)
    if txn is not None and txn.side == Side.BPP:
        update_transaction(txn, status=action.upper(), status_message_id=msg.message_id)
    return msg


def record_nack(payload: dict, action: str, nack_body: dict, side: str) -> dict:
    context = payload.get("context") if isinstance(payload, dict) else None
    context = context if isinstance(context, dict) else {}
    sender = inbound_sender(context, side, payload)
    if find_inbound(context, action, sender) is None:
        record_inbound(None, payload if isinstance(payload, dict) else {}, action, nack_body, sender, Processing.FAILED)
    return nack_body


def claim_inbound(message_pk: int) -> BecknMessage | None:
    msg = BecknMessage.objects.select_for_update(of=("self",)).select_related("transaction").get(pk=message_pk)
    return msg if msg.processing_status == Processing.PENDING else None


def finish_inbound(msg: BecknMessage, status: str = Processing.PROCESSED) -> None:
    msg.processing_status = status
    msg.save(update_fields=["processing_status", "modified_date"])


def _as_dict(message: Any) -> dict:
    return message.dump() if hasattr(message, "dump") else dict(message or {})


def record_outbound(
    txn: BecknTransaction,
    context: dict,
    message: Any,
    error: dict | None = None,
    reply_to: BecknMessage | None = None,
) -> BecknMessage:
    payload: dict[str, Any] = {"context": context, "message": _as_dict(message)}
    if error:
        payload["error"] = error
    msg = BecknMessage.objects.create(
        transaction=txn,
        message_id=context["messageId"],
        action=context["action"],
        direction=Direction.OUT,
        sender_id="",
        payload=payload,
        reply_to=reply_to,
        delivery_status=Delivery.QUEUED,
    )
    if txn.side == Side.BAP and not is_callback(msg.action):
        update_transaction(
            txn,
            status=TxnStatus.QUEUED,
            status_message_id=msg.message_id,
            last_error=None,
            **({"network_id": context["networkId"]} if context.get("networkId") else {}),
        )
    return msg


def record_unsolicited(
    txn: BecknTransaction, flow, action: str, message: Any, error: dict | None = None
) -> BecknMessage:
    routing = {"bapId": txn.counterparty_id, "bapUri": txn.counterparty_uri}
    context = flow.context().unsolicited(action, txn.transaction_id, routing)
    msg = record_outbound(txn, context, message, error)
    enqueue_delivery(msg)
    return msg


def record_error_callback(message_pk: int, code: str, text: str) -> BecknMessage | None:
    from care_ccn_be.flows.registry import FlowRegistry

    msg = BecknMessage.objects.select_related("transaction").filter(pk=message_pk).first()
    if msg is None or msg.transaction is None:
        return None
    flow = FlowRegistry.get(msg.transaction.flow)
    payload = msg.payload
    context = flow.context().callback(payload.get("context") or {}, msg.action)
    error = {"type": "DOMAIN-ERROR", "code": code, "message": text}
    with transaction.atomic():
        out = record_outbound(msg.transaction, context, payload.get("message") or {}, error, reply_to=msg)
        finish_inbound(msg, Processing.FAILED)
        enqueue_delivery(out)
    return out


# ----------------------------------------------------------------------------- delivery
def enqueue_delivery(msg: BecknMessage) -> None:
    from care_ccn_be.tasks import deliver_beckn_message

    queue = plugin_settings.BECKN_CELERY_QUEUE or None
    transaction.on_commit(partial(deliver_beckn_message.apply_async, args=[msg.pk], queue=queue), robust=True)


def mark_attempt(msg: BecknMessage) -> None:
    BecknMessage.objects.filter(pk=msg.pk).update(attempts=F("attempts") + 1)
    msg.refresh_from_db(fields=["attempts"])


def _owns_status(txn: BecknTransaction | None, msg: BecknMessage) -> bool:
    if txn is None:
        return False
    if txn.status_message_id == msg.message_id:
        return True
    return bool(msg.reply_to_id and txn.status_message_id == msg.reply_to.message_id)


def apply_delivery(msg: BecknMessage, result: AckResult) -> None:
    # re-read under lock: a fast callback may already have moved the transaction past this request
    txn = lock(msg.transaction) if msg.transaction_id else None
    if result.result == SKIPPED:
        msg.delivery_status = Delivery.SKIPPED
        msg.last_error = result.detail.get("reason", "")
        if txn is not None and txn.side == Side.BAP and _owns_status(txn, msg):
            update_transaction(
                txn, status=TxnStatus.ERROR, last_error={"code": "NOT_CONFIGURED", "message": msg.last_error}
            )
    elif result.result == ACK:
        msg.delivery_status = Delivery.ACKED
        msg.ack = result.detail.get("body") if isinstance(result.detail.get("body"), dict) else result.detail
        if txn is not None and _owns_status(txn, msg):
            if txn.side == Side.BAP and txn.status == TxnStatus.QUEUED:
                update_transaction(txn, status=msg.action.upper())
            elif txn.side == Side.BPP and is_callback(msg.action):
                update_transaction(txn, status=msg.action.upper())
    else:
        msg.delivery_status = Delivery.NACKED
        msg.ack = result.detail
        msg.last_error = json.dumps(result.detail.get("error") or result.detail, default=str)[:2000]
        if txn is not None and _owns_status(txn, msg):
            update_transaction(txn, status=TxnStatus.NACK, last_error=result.detail.get("error") or result.detail)
    msg.save(update_fields=["delivery_status", "ack", "last_error", "modified_date"])


def mark_failed(msg: BecknMessage, error: str) -> None:
    msg.delivery_status = Delivery.FAILED
    msg.last_error = error[:2000]
    msg.save(update_fields=["delivery_status", "last_error", "modified_date"])
    txn = lock(msg.transaction) if msg.transaction_id else None
    if txn is not None and _owns_status(txn, msg):
        update_transaction(txn, status=TxnStatus.ERROR, last_error={"code": "DELIVERY_FAILED", "message": error[:500]})


# ----------------------------------------------------------------------------- callbacks (BAP)
def _answers_latest_request(txn: BecknTransaction, msg: BecknMessage) -> bool:
    latest = BecknMessage.objects.filter(
        transaction=txn, direction=Direction.OUT, message_id=txn.status_message_id
    ).first()
    return latest is not None and latest.action == request_of(msg.action)


def apply_callback(msg: BecknMessage) -> BecknTransaction:
    txn = msg.transaction
    context = msg.payload.get("context") or {}
    contract = (msg.payload.get("message") or {}).get("contract") or {}
    fields: dict[str, Any] = {}
    routing = extract_routing(context)
    if msg.action != callback_of("discover") and routing["bppId"]:
        fields.update(counterparty_id=routing["bppId"], counterparty_uri=routing["bppUri"] or txn.counterparty_uri)
    if isinstance(contract, dict) and contract.get("id"):
        fields["contract_id"] = str(contract["id"])
    error = msg.payload.get("error")
    if msg.action in UNSOLICITED_CALLBACKS or _answers_latest_request(txn, msg):
        if error:
            fields.update(status=TxnStatus.ERROR, last_error=error)
        else:
            fields.update(status=msg.action.upper())
    if fields:
        update_transaction(txn, **fields)
    return txn


# ----------------------------------------------------------------------------- read side
def latest_payload(txn: BecknTransaction, action: str) -> dict | None:
    msg = BecknMessage.objects.filter(transaction=txn, action=action.lower()).order_by("-pk").first()
    return msg.payload if msg else None


def merged_catalogs(txn: BecknTransaction) -> list[dict]:
    catalogs: list[dict] = []
    for msg in BecknMessage.objects.filter(transaction=txn, direction=Direction.IN, action="on_discover").order_by(
        "pk"
    ):
        catalogs.extend((msg.payload.get("message") or {}).get("catalogs") or [])
    return catalogs


def actions_seen(txn: BecknTransaction) -> list[str]:
    seen: list[str] = []
    for action in BecknMessage.objects.filter(transaction=txn).order_by("pk").values_list("action", flat=True):
        if action.upper() not in seen:
            seen.append(action.upper())
    return seen


# ----------------------------------------------------------------------------- housekeeping
def stuck_outbound():
    return BecknMessage.objects.filter(
        direction=Direction.OUT, delivery_status=Delivery.QUEUED, modified_date__lt=timezone.now() - STUCK_AFTER
    )


def stuck_inbound():
    return BecknMessage.objects.filter(
        direction=Direction.IN, processing_status=Processing.PENDING, modified_date__lt=timezone.now() - STUCK_AFTER
    )


def purge_messages(days: int) -> int:
    if days <= 0:
        return 0
    deleted, _ = BecknMessage.objects.filter(created_date__lt=timezone.now() - timedelta(days=days)).delete()
    return deleted
