import logging

import requests
from celery import shared_task
from django.db import transaction

from care_ccn_be import store
from care_ccn_be.flows.referral import ReferralFlow
from care_ccn_be.models import Adapter, BecknMessage, BecknTransaction, Direction, Processing
from care_ccn_be.models.referral import ReferralLink
from care_ccn_be.protocol.actions import CONFIRM, CONTRACT_ACTIVE, UPDATE, callback_of
from care_ccn_be.resources.builders.referral import CareReferral, ReferralCareConfirmBuilder, completion_message
from care_ccn_be.services import artifacts, referral_sync
from care_ccn_be.services import availability as availabilities
from care_ccn_be.services import referral as lifecycle
from care_ccn_be.settings import plugin_settings

logger = logging.getLogger(__name__)


@shared_task(name="care_ccn_be.referral.submit")
def submit_referral(referral_id: int) -> str:
    """Send a Referral created in Care on a network availability to that target as a confirm."""
    from care.emr.models.referral import Referral

    flow = ReferralFlow
    referral = (
        Referral.objects.select_related("availability", "patient", "encounter__facility", "created_by")
        .filter(pk=referral_id)
        .first()
    )
    if referral is None or ReferralLink.objects.filter(referral=referral, active=True).exists():
        return "skipped"
    target = availabilities.target_of(referral.availability)
    if not target:
        return "skipped"
    coordination_id = str(referral.external_id)
    with transaction.atomic():
        txn = store.open_bap_transaction(
            flow, referral.created_by, transaction_id=coordination_id, coordination_id=coordination_id
        )
        default = flow.context().default_bpp()
        store.update_transaction(
            txn,
            counterparty_id=target.get("bppId") or default.id,
            counterparty_uri=target.get("bppUri") or default.uri,
        )
        care_referral = CareReferral(referral, coordination_id)
        lifecycle.adopt(txn, lifecycle.ORIGIN, referral, {"originFacilityId": care_referral.origin_facility_id})
        message = ReferralCareConfirmBuilder().message(care_referral)
        routing = {"bppId": txn.counterparty_id, "bppUri": txn.counterparty_uri}
        msg = store.record_outbound(txn, flow.context().request("confirm", coordination_id, routing), message)
        store.enqueue_delivery(msg)
    return "sent"


@shared_task(name="care_ccn_be.referral.complete")
def complete_referral(coordination_id: str) -> str:
    """A fulfilled booking completes the assigned referral and tells the referring side (``on_update``)."""
    flow = ReferralFlow
    txn = BecknTransaction.objects.filter(flow=flow.name, adapter=Adapter.BPP, coordination_id=coordination_id).first()
    if txn is None:
        return "no-referral"
    with transaction.atomic():
        txn = store.lock(txn)
        assigned = lifecycle.current(coordination_id, lifecycle.ASSIGNED)
        if assigned is not None and lifecycle.complete(coordination_id, lifecycle.ASSIGNED) is None:
            return "already-completed"
        if not (txn.counterparty_id and txn.counterparty_uri):
            return "no-routing"
        store.record_unsolicited(txn, flow, "update", completion_message(coordination_id))
    return "completed"


def confirmed(txn: BecknTransaction, link: ReferralLink) -> bool:
    """An update must not overtake the confirm that creates the other side's referral."""
    if (link.details or {}).get("confirmed") is True:
        return True
    callback = (
        BecknMessage.objects.filter(
            transaction=txn,
            direction=Direction.IN,
            action=callback_of(CONFIRM),
            processing_status=Processing.PROCESSED,
            payload__error__isnull=True,
            payload__message__contract__status__code=CONTRACT_ACTIVE,
        )
        .order_by("-pk")
        .first()
    )
    if callback is None:
        return False
    lifecycle.remember_confirmation(link, callback.payload.get("message") or {})
    return True


@shared_task(name="care_ccn_be.referral.sync")
def sync_referral(referral_id: int) -> str:
    """Send what was added on a linked referral to the other side: the referring side as an ``update``, the
    receiving side as an unsolicited ``on_update``."""
    flow = ReferralFlow
    link = ReferralLink.objects.select_related("transaction").filter(referral_id=referral_id, active=True).first()
    if link is None:
        return "unlinked"
    coordination_id = link.transaction.coordination_id or link.beckn_ref
    adapter = Adapter.BAP if link.role == lifecycle.ORIGIN else Adapter.BPP
    txn = (
        BecknTransaction.objects.filter(flow=flow.name, adapter=adapter, coordination_id=coordination_id)
        .order_by("pk")
        .first()
    )
    if txn is None:
        return "no-route"
    with transaction.atomic():
        txn = store.lock(txn)
        link = ReferralLink.objects.select_related("referral").get(pk=link.pk)
        if adapter == Adapter.BAP and not confirmed(txn, link):
            return "not-confirmed"
        if not (txn.counterparty_id and txn.counterparty_uri):
            return "no-routing"
        outbox = referral_sync.pending(link)
        if not outbox:
            return "noop"
        if adapter == Adapter.BAP:
            message = referral_sync.message(
                coordination_id,
                outbox,
                txn.contract_id or coordination_id,
                (link.details or {}).get("external_referral_id"),
            )
            routing = {"bppId": txn.counterparty_id, "bppUri": txn.counterparty_uri}
            overrides = {
                key: value
                for key, value in store.get_state(txn, flow).context_overrides.items()
                if key not in ("bppId", "bppUri")
            }
            context = flow.context().request(UPDATE, txn.transaction_id, routing, overrides)
            store.enqueue_delivery(store.record_outbound(txn, context, message))
        else:
            store.record_unsolicited(txn, flow, UPDATE, referral_sync.message(coordination_id, outbox))
        referral_sync.mark_sent(link, outbox)
    return "sent"


@shared_task(name="care_ccn_be.referral.publish_catalog")
def publish_referral_catalog(facility_id: int | None = None, availability_id: int | None = None) -> str:
    """Publish the coordinator catalog of ``facility_id`` (every facility's when ``None``, as a full update).

    The catalog keeps its id: while ``availability_id`` is published it is merged into the facility's catalog
    already on the network (added, or updated in place); once withdrawn (private, inactive or deleted) the
    facility's catalog is replaced so it drops out."""
    from care_ccn_be.services.publisher import UPDATE_MODE_FULL, UPDATE_MODE_MERGE, publish_catalog

    if facility_id is None:
        result = publish_catalog(ReferralFlow)
        return f"queued {len(result.catalogs)}"
    listed = (
        availability_id is not None and availabilities.receiving(public_only=True).filter(pk=availability_id).exists()
    )
    mode = UPDATE_MODE_MERGE if listed else UPDATE_MODE_FULL
    result = publish_catalog(ReferralFlow, facility_ids=[facility_id], update_mode=mode)
    return f"queued {len(result.catalogs)} ({mode})" if result.message else "skipped"


@shared_task(
    name="care_ccn_be.referral.import_file",
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=plugin_settings.BECKN_MAX_RETRIES,
)
def import_referral_file(referral_id: int, document: dict, attachment: dict) -> str:
    """Download a document the other side shared and attach it to the local referral."""
    from care.emr.models.referral import Referral

    referral = Referral.objects.filter(pk=referral_id).first()
    if referral is None:
        return "skipped"
    if artifacts.imported(referral, attachment["ref"]):
        return "duplicate"
    try:
        artifacts.validate(document, attachment)
        content = artifacts.download(document.get("url"))
    except artifacts.ArtifactRejected as exc:
        logger.warning("referral document %s rejected: %s", attachment["ref"], exc)
        return "rejected"
    with transaction.atomic():
        referral = Referral.objects.select_for_update().get(pk=referral_id)
        if artifacts.imported(referral, attachment["ref"]):
            return "duplicate"
        artifacts.save(referral, document, attachment, content)
    return "imported"
