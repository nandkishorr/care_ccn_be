from celery import shared_task
from django.db import transaction

from care_ccn_be import store
from care_ccn_be.flows.referral import ReferralFlow
from care_ccn_be.models import Adapter, BecknTransaction
from care_ccn_be.models.referral import ReferralLink
from care_ccn_be.resources.builders.referral import CareReferral, ReferralCareConfirmBuilder, completion_message
from care_ccn_be.services import availability as availabilities
from care_ccn_be.services import referral as lifecycle


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
