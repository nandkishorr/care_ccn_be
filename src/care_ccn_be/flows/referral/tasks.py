from celery import shared_task
from django.db import transaction

from care_ccn_be import store
from care_ccn_be.flows.referral import lifecycle
from care_ccn_be.flows.referral.builders import CareReferral, ReferralCareConfirmBuilder, completion_message
from care_ccn_be.flows.referral.models import ReferralLink
from care_ccn_be.flows.registry import FlowRegistry
from care_ccn_be.models import BecknTransaction, Side


@shared_task(name="care_ccn_be.referral.submit")
def submit_referral(resource_request_id: int) -> str:
    """Send a ResourceRequest created in Care to its target (``extensions.beckn.target``, else the CC) as a confirm."""
    from care.emr.models.resource_request import ResourceRequest

    flow = FlowRegistry.get("referral")
    request = (
        ResourceRequest.objects.select_related("origin_facility", "assigned_facility", "related_patient", "created_by")
        .filter(pk=resource_request_id)
        .first()
    )
    if request is None or ReferralLink.objects.filter(resource_request=request, active=True).exists():
        return "skipped"
    coordination_id = str(request.external_id)
    with transaction.atomic():
        txn = store.open_bap_transaction(
            flow, request.created_by, transaction_id=coordination_id, coordination_id=coordination_id
        )
        target = (request.extensions or {}).get("beckn", {}).get("target") or {}
        default = flow.context().default_bpp()
        store.update_transaction(
            txn,
            counterparty_id=target.get("bppId") or default.id,
            counterparty_uri=target.get("bppUri") or default.uri,
        )
        lifecycle.adopt(txn, lifecycle.ORIGIN, request)
        message = ReferralCareConfirmBuilder().message(CareReferral(request, coordination_id))
        routing = {"bppId": txn.counterparty_id, "bppUri": txn.counterparty_uri}
        msg = store.record_outbound(txn, flow.context().request("confirm", coordination_id, routing), message)
        store.enqueue_delivery(msg)
    return "sent"


@shared_task(name="care_ccn_be.referral.complete")
def complete_referral(coordination_id: str) -> str:
    """A fulfilled booking completes the assigned request and tells the referring side (``on_update``)."""
    flow = FlowRegistry.get("referral")
    txn = BecknTransaction.objects.filter(flow=flow.name, side=Side.BPP, coordination_id=coordination_id).first()
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
