"""Celery tasks. Every send is persisted first (``store.record_outbound``) and delivered here after commit."""

import logging
from datetime import timedelta

from celery import Task, current_app, shared_task
from celery.schedules import crontab
from django.db import transaction

from care_ccn_be import store
from care_ccn_be.models import BecknMessage, Delivery, Processing, Side
from care_ccn_be.protocol.client import BecknClient, TransportError
from care_ccn_be.settings import plugin_settings

logger = logging.getLogger(__name__)


class DeliveryTask(Task):
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        msg = BecknMessage.objects.select_related("transaction").filter(pk=args[0]).first()
        if msg is not None and msg.delivery_status == Delivery.QUEUED:
            with transaction.atomic():
                store.mark_failed(msg, str(exc))


def caller_url(msg: BecknMessage) -> str:
    """A BPP-side transaction (and every ``catalog/publish``) sends through the BPP caller."""
    txn = msg.transaction
    if msg.action == "catalog/publish" or (txn is not None and txn.side == Side.BPP):
        return plugin_settings.BECKN_BPP_CALLER_URL
    return plugin_settings.BECKN_BAP_CALLER_URL


@shared_task(
    bind=True,
    base=DeliveryTask,
    name="care_ccn_be.deliver_beckn_message",
    autoretry_for=(TransportError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=plugin_settings.BECKN_MAX_RETRIES,
)
def deliver_beckn_message(self, message_pk: int) -> str:
    msg = BecknMessage.objects.select_related("transaction").filter(pk=message_pk).first()
    if msg is None or msg.delivery_status != Delivery.QUEUED:
        return "noop"
    store.mark_attempt(msg)
    client = BecknClient(caller_url(msg), plugin_settings.BECKN_HTTP_TIMEOUT)
    result = client.post(msg.action, msg.payload)
    if result.retryable:
        raise TransportError(str(result.detail)[:500])
    with transaction.atomic():
        store.apply_delivery(msg, result)
    return result.result


@shared_task(name="care_ccn_be.process_bpp_request")
def process_bpp_request(message_pk: int) -> str:
    """Run the BPP service for one stored inbound request and queue its callback."""
    from care_ccn_be.bpp.service import process_inbound

    try:
        return process_inbound(message_pk)
    except Exception as exc:  # noqa: BLE001 - the message must never stay pending forever
        logger.exception("process_bpp_request(%s) failed", message_pk)
        store.record_error_callback(message_pk, "PROCESSING_FAILED", str(exc)[:500])
        return "failed"


@shared_task(name="care_ccn_be.process_bap_callback")
def process_bap_callback(message_pk: int) -> str:
    """Apply one stored inbound ``on_*`` callback to its BAP transaction and run its flow hook."""
    from care_ccn_be.bap.receiver import process_callback

    try:
        return process_callback(message_pk)
    except Exception:
        logger.exception("process_bap_callback(%s) failed", message_pk)
        with transaction.atomic():
            msg = store.claim_inbound(message_pk)
            if msg is not None:
                store.finish_inbound(msg, Processing.FAILED)
        return "failed"


@shared_task(name="care_ccn_be.requeue_stuck_messages")
def requeue_stuck_messages() -> int:
    count = 0
    for msg in store.stuck_outbound():
        store.enqueue_delivery(msg)
        count += 1
    for msg in store.stuck_inbound():
        txn = msg.transaction
        if txn is None:
            continue
        task = process_bpp_request if txn.side == Side.BPP else process_bap_callback
        task.apply_async(args=[msg.pk], queue=plugin_settings.BECKN_CELERY_QUEUE or None)
        count += 1
    return count


@shared_task(name="care_ccn_be.purge_beckn_messages")
def purge_beckn_messages() -> int:
    return store.purge_messages(plugin_settings.BECKN_MESSAGE_RETENTION_DAYS)


@current_app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(timedelta(minutes=5), requeue_stuck_messages.s(), name="care_ccn_be.requeue_stuck")
    sender.add_periodic_task(crontab(hour=3, minute=30), purge_beckn_messages.s(), name="care_ccn_be.purge")
